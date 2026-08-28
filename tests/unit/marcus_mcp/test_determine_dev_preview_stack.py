"""
Unit tests for src/marcus_mcp/server.py's repo-aware dev-preview stack
resolution: _get_repo_stack_cache_mgr, _repo_fingerprint,
_detect_stack_from_repo_only, _maybe_ai_infer_stack,
_maybe_update_dev_preview_readme, and the top-level orchestrator
_determine_dev_preview_stack.

Regression coverage for the gap this closes: dev-environment preview
start used to hard-block with "fill in the Tech Stack section" whenever
a project had no description at all, even when the repo's own files (or
an AI reading them) could have answered the question — and even a
DECLARED stack was only ever cross-checked against the repo when
file-sniffing found something specific enough to compare, so an
unrecognized layout silently fell back to serving static files forever.

Uses real temporary git-free repo directories (plain files — nothing
here needs git history) and a real ProjectDescriptionManager, matching
this codebase's established testing style for file-content-driven logic;
the AI call and the README git flow are each independently covered by
their own dedicated test files
(test_repo_stack_inference.py, test_repo_readme_writer.py) so here they
are patched at their call sites to keep these tests fast and focused on
the ORCHESTRATION logic itself.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.project_description import ProjectDescriptionManager, ProjectStack
from src.core.repo_stack_cache import RepoStackCache
from src.marcus_mcp.server import (
    _detect_stack_from_repo_only,
    _determine_dev_preview_stack,
    _get_repo_stack_cache_mgr,
    _maybe_ai_infer_stack,
    _maybe_update_dev_preview_readme,
    _persist_detected_stack,
    _repo_fingerprint,
)


def _mgr(tmp_path: Path) -> ProjectDescriptionManager:
    return ProjectDescriptionManager(data_dir=tmp_path / "descriptions")


def _server(tmp_path: Path, **kwargs) -> SimpleNamespace:
    """Build a stub server with its RepoStackCache singleton pre-set to an
    isolated tmp_path — _get_repo_stack_cache_mgr then reuses this
    instance instead of constructing one against the real project's
    ./data/ directory (which would leak test state into real files)."""
    server = SimpleNamespace(**kwargs)
    server._repo_stack_cache_mgr = RepoStackCache(data_dir=tmp_path / "cache")
    return server


def _django_stack() -> ProjectStack:
    return ProjectStack(
        language="python", framework="Django",
        install_cmd="pip install -r requirements.txt",
        dev_cmd="python manage.py runserver 0.0.0.0:3000",
    )


class TestPersistDetectedStack:
    """Regression: _persist_corrected_stack only ever PATCHES an existing
    description and is a deliberate no-op when none exists — exactly the
    common case here (no description at all yet). _persist_detected_stack
    must create one from scratch in that case."""

    def test_creates_a_fresh_description_when_none_exists(self, tmp_path):
        mgr = _mgr(tmp_path)

        _persist_detected_stack(mgr, 7, _django_stack())

        text = mgr.get_description(7)
        assert text is not None
        assert "Django" in text
        assert "python manage.py runserver 0.0.0.0:3000" in text

    def test_patches_the_tech_stack_section_when_a_description_exists(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.update_description(
            7,
            "# Demo\n\n## Overview\nSomething.\n\n## Tech Stack\n"
            "- **Language**: Python\n"
            "- **Dev server command**: python -m http.server 3000\n"
            "- **Install command**: pip install -r requirements.txt\n\n"
            "## Architecture Notes\nFabricated details.\n",
        )

        _persist_detected_stack(mgr, 7, _django_stack())

        text = mgr.get_description(7)
        assert "Django" in text
        assert "Fabricated details." in text  # other sections untouched

    def test_never_raises_on_a_broken_desc_mgr(self, tmp_path):
        mgr = MagicMock()
        mgr.get_description.side_effect = RuntimeError("disk on fire")

        _persist_detected_stack(mgr, 7, _django_stack())  # must not raise


class TestGetRepoStackCacheMgr:
    def test_constructs_once_and_caches_on_server(self):
        server = SimpleNamespace()
        with patch("src.core.repo_stack_cache.RepoStackCache") as cache_cls:
            cache_cls.return_value = MagicMock()
            first = _get_repo_stack_cache_mgr(server)
            second = _get_repo_stack_cache_mgr(server)

        cache_cls.assert_called_once()
        assert first is second
        assert server._repo_stack_cache_mgr is first

    def test_reuses_a_preexisting_instance(self):
        server = SimpleNamespace()
        existing = MagicMock()
        server._repo_stack_cache_mgr = existing

        with patch("src.core.repo_stack_cache.RepoStackCache") as cache_cls:
            result = _get_repo_stack_cache_mgr(server)

        cache_cls.assert_not_called()
        assert result is existing


class TestRepoFingerprint:
    @pytest.mark.asyncio
    async def test_returns_head_sha_for_a_real_repo(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-m", "x"], cwd=tmp_path, check=True, capture_output=True)
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

        result = await _repo_fingerprint(str(tmp_path))

        assert result == expected

    @pytest.mark.asyncio
    async def test_returns_empty_string_for_non_git_dir(self, tmp_path):
        result = await _repo_fingerprint(str(tmp_path))
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_string_for_nonexistent_path(self, tmp_path):
        result = await _repo_fingerprint(str(tmp_path / "nope"))
        assert result == ""


class TestDetectStackFromRepoOnly:
    def test_detects_nodejs_with_no_description_at_all(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "app"}')
        mgr = _mgr(tmp_path)

        result = _detect_stack_from_repo_only(mgr, 7, str(repo))

        assert result is not None
        assert result.language == "nodejs"
        assert result.install_cmd == "npm install"

    def test_persists_the_detected_stack_into_the_description(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("Django>=4.2\n")
        (repo / "manage.py").write_text("#!/usr/bin/env python\n")
        mgr = _mgr(tmp_path)

        _detect_stack_from_repo_only(mgr, 7, str(repo))

        text = mgr.get_description(7)
        assert text is not None
        assert "Django" in text

    def test_returns_none_when_nothing_recognizable(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "mystery.xyz").write_text("???")
        mgr = _mgr(tmp_path)

        result = _detect_stack_from_repo_only(mgr, 7, str(repo))

        assert result is None

    def test_works_with_no_desc_mgr(self, tmp_path):
        """desc_mgr=None (caller couldn't resolve a project id) must not
        crash — it just means the detected stack is used for THIS
        preview only, never persisted."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text("{}")

        result = _detect_stack_from_repo_only(None, 7, str(repo))

        assert result is not None
        assert result.language == "nodejs"


class TestMaybeAiInferStack:
    @pytest.mark.asyncio
    async def test_returns_resolved_unchanged_when_no_ai_engine(self, tmp_path):
        server = _server(tmp_path, ai_engine=None)
        repo = tmp_path / "repo"
        repo.mkdir()

        result = await _maybe_ai_infer_stack(server, None, 7, None, str(repo))

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_resolved_unchanged_when_no_repo_path(self):
        server = SimpleNamespace(ai_engine=MagicMock())
        existing = _django_stack()

        result = await _maybe_ai_infer_stack(server, None, 7, existing, None)

        assert result is existing

    @pytest.mark.asyncio
    async def test_uses_ai_inferred_stack_and_caches_it(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        # Cache storage is keyed to the repo's commit SHA (see
        # _repo_fingerprint), so this needs real git history — a
        # fingerprint-less repo deliberately skips caching (nothing to
        # detect staleness against).
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "x"], cwd=repo, check=True, capture_output=True)
        ai_engine = SimpleNamespace(generate_text=AsyncMock(return_value="{}"))
        server = _server(tmp_path, ai_engine=ai_engine)
        mgr = _mgr(tmp_path)

        with patch(
            "src.core.repo_stack_inference.infer_stack_with_ai",
            new=AsyncMock(return_value=_django_stack()),
        ) as fake_infer:
            result = await _maybe_ai_infer_stack(server, mgr, 7, None, str(repo))

        assert result is not None
        assert result.framework == "Django"
        fake_infer.assert_awaited_once()

        cache = _get_repo_stack_cache_mgr(server)
        assert cache.get_ai_stack(7) is not None

    @pytest.mark.asyncio
    async def test_second_call_uses_cache_not_a_fresh_ai_call(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "x"], cwd=repo, check=True, capture_output=True)

        ai_engine = SimpleNamespace(generate_text=AsyncMock(return_value="{}"))
        server = _server(tmp_path, ai_engine=ai_engine)
        mgr = _mgr(tmp_path)

        with patch(
            "src.core.repo_stack_inference.infer_stack_with_ai",
            new=AsyncMock(return_value=_django_stack()),
        ) as fake_infer:
            await _maybe_ai_infer_stack(server, mgr, 7, None, str(repo))
            await _maybe_ai_infer_stack(server, mgr, 7, None, str(repo))

        fake_infer.assert_awaited_once()  # NOT called a second time

    @pytest.mark.asyncio
    async def test_ai_returning_none_falls_back_to_resolved(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        ai_engine = SimpleNamespace(generate_text=AsyncMock(return_value="{}"))
        server = _server(tmp_path, ai_engine=ai_engine)

        with patch(
            "src.core.repo_stack_inference.infer_stack_with_ai",
            new=AsyncMock(return_value=None),
        ):
            result = await _maybe_ai_infer_stack(server, None, 7, None, str(repo))

        assert result is None

    @pytest.mark.asyncio
    async def test_exception_during_inference_falls_back_to_resolved_not_a_crash(
        self, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        ai_engine = SimpleNamespace(generate_text=AsyncMock(return_value="{}"))
        server = _server(tmp_path, ai_engine=ai_engine)
        existing = _django_stack()

        with patch(
            "src.core.repo_stack_inference.infer_stack_with_ai",
            side_effect=RuntimeError("boom"),
        ):
            result = await _maybe_ai_infer_stack(server, None, 7, existing, str(repo))

        assert result is existing


class TestMaybeUpdateDevPreviewReadme:
    @pytest.mark.asyncio
    async def test_writes_when_hash_not_cached(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        server = _server(tmp_path)

        with patch(
            "src.core.repo_readme_writer.update_dev_preview_readme_section",
            new=AsyncMock(return_value=True),
        ) as fake_write:
            await _maybe_update_dev_preview_readme(server, 7, _django_stack(), str(repo))

        fake_write.assert_awaited_once()
        cache = _get_repo_stack_cache_mgr(server)
        assert cache.get_readme_hash(7) is not None

    @pytest.mark.asyncio
    async def test_skips_write_when_hash_already_matches(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        server = _server(tmp_path)
        cache = _get_repo_stack_cache_mgr(server)
        from src.core.repo_stack_cache import stack_hash

        cache.store_readme_hash(7, stack_hash(_django_stack()))

        with patch(
            "src.core.repo_readme_writer.update_dev_preview_readme_section",
            new=AsyncMock(return_value=True),
        ) as fake_write:
            await _maybe_update_dev_preview_readme(server, 7, _django_stack(), str(repo))

        fake_write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rewrites_when_stack_changed(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        server = _server(tmp_path)
        cache = _get_repo_stack_cache_mgr(server)
        from src.core.repo_stack_cache import stack_hash

        cache.store_readme_hash(7, stack_hash(_django_stack()))
        flask_stack = ProjectStack(
            language="python", framework="Flask",
            install_cmd="pip install -r requirements.txt",
            dev_cmd="flask run --host 0.0.0.0 --port 3000",
        )

        with patch(
            "src.core.repo_readme_writer.update_dev_preview_readme_section",
            new=AsyncMock(return_value=True),
        ) as fake_write:
            await _maybe_update_dev_preview_readme(server, 7, flask_stack, str(repo))

        fake_write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_write_failure_never_raises(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        server = _server(tmp_path)

        with patch(
            "src.core.repo_readme_writer.update_dev_preview_readme_section",
            side_effect=RuntimeError("push failed"),
        ):
            await _maybe_update_dev_preview_readme(server, 7, _django_stack(), str(repo))
        # no exception propagated — test passes just by reaching here


class TestDetermineDevPreviewStack:
    @pytest.mark.asyncio
    async def test_declared_stack_matching_repo_is_used_unchanged_no_ai_call(
        self, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text("{}")
        server = _server(tmp_path, ai_engine=None)
        mgr = _mgr(tmp_path)
        declared = ProjectStack(
            language="nodejs", framework="Express",
            install_cmd="npm install", dev_cmd="npm start",
        )

        with patch(
            "src.core.repo_readme_writer.update_dev_preview_readme_section",
            new=AsyncMock(return_value=False),
        ):
            result = await _determine_dev_preview_stack(
                server, mgr, 7, declared, str(repo)
            )

        assert result is declared

    @pytest.mark.asyncio
    async def test_no_declared_stack_detects_from_repo_files(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text("{}")
        server = _server(tmp_path, ai_engine=None)
        mgr = _mgr(tmp_path)

        with patch(
            "src.core.repo_readme_writer.update_dev_preview_readme_section",
            new=AsyncMock(return_value=False),
        ):
            result = await _determine_dev_preview_stack(server, mgr, 7, None, str(repo))

        assert result is not None
        assert result.language == "nodejs"

    @pytest.mark.asyncio
    async def test_falls_back_to_ai_when_repo_unrecognized(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "mystery.xyz").write_text("some code")
        ai_engine = SimpleNamespace(generate_text=AsyncMock(return_value="{}"))
        server = _server(tmp_path, ai_engine=ai_engine)
        mgr = _mgr(tmp_path)

        with (
            patch(
                "src.core.repo_stack_inference.infer_stack_with_ai",
                new=AsyncMock(return_value=_django_stack()),
            ),
            patch(
                "src.core.repo_readme_writer.update_dev_preview_readme_section",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await _determine_dev_preview_stack(server, mgr, 7, None, str(repo))

        assert result is not None
        assert result.framework == "Django"

    @pytest.mark.asyncio
    async def test_returns_none_when_everything_fails(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "mystery.xyz").write_text("some code")
        server = _server(tmp_path, ai_engine=None)  # no AI available either
        mgr = _mgr(tmp_path)

        result = await _determine_dev_preview_stack(server, mgr, 7, None, str(repo))

        assert result is None

    @pytest.mark.asyncio
    async def test_updates_readme_when_a_stack_was_resolved(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text("{}")
        server = _server(tmp_path, ai_engine=None)
        mgr = _mgr(tmp_path)

        with patch(
            "src.core.repo_readme_writer.update_dev_preview_readme_section",
            new=AsyncMock(return_value=True),
        ) as fake_write:
            await _determine_dev_preview_stack(server, mgr, 7, None, str(repo))

        fake_write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_nonexistent_repo_path_with_no_declared_stack_returns_none(
        self, tmp_path
    ):
        server = _server(tmp_path, ai_engine=None)
        mgr = _mgr(tmp_path)

        result = await _determine_dev_preview_stack(
            server, mgr, 7, None, str(tmp_path / "does-not-exist")
        )

        assert result is None
