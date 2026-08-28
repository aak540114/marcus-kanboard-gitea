"""
Unit tests for src/core/repo_readme_writer.py.

_render_section / _apply_section are pure-function tests. The full
update_dev_preview_readme_section flow is exercised against a REAL local
git remote (a bare repo under tmp_path) and a real working clone — no
mocked git commands — matching this codebase's established "empirically
verify against the real implementation" testing style for anything
touching git/file content (see test_reconcile_stack_with_repo.py's own
docstring for the same rationale). All operations are against local
file:// paths, so this stays fast with no network involved.
"""

import subprocess
from pathlib import Path

import pytest

from src.core.project_description import ProjectStack
from src.core.repo_readme_writer import (
    _apply_section,
    _render_section,
    update_dev_preview_readme_section,
)


def _django_stack() -> ProjectStack:
    return ProjectStack(
        language="python",
        framework="Django",
        install_cmd="pip install --break-system-packages -r requirements.txt",
        dev_cmd="python manage.py runserver 0.0.0.0:3000",
        use_hm_reload=False,
    )


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _init_bare_origin_with_working_clone(tmp_path):
    """Set up a bare "origin" repo plus a real working clone pointed at
    it — the same shape Marcus's own GiteaManager.init_with_readme
    leaves behind, minus the actual Gitea server.

    Returns (bare_repo_path, working_clone_path).
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _run(["git", "init", "--bare", "-b", "main"], cwd=str(bare))

    working = tmp_path / "working"
    working.mkdir()
    _run(["git", "init", "-b", "main"], cwd=str(working))
    _run(["git", "config", "user.email", "test@example.com"], cwd=str(working))
    _run(["git", "config", "user.name", "Test"], cwd=str(working))
    (working / "README.md").write_text("# My App\n\nManaged by Marcus.\n")
    _run(["git", "add", "README.md"], cwd=str(working))
    _run(["git", "commit", "-m", "init"], cwd=str(working))
    _run(["git", "remote", "add", "origin", str(bare)], cwd=str(working))
    _run(["git", "push", "-u", "origin", "main"], cwd=str(working))

    return str(bare), str(working)


def _readme_on_bare_main(bare_path: str) -> str:
    result = subprocess.run(
        ["git", "show", "main:README.md"],
        cwd=bare_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class TestRenderSection:
    def test_includes_key_stack_fields(self):
        section = _render_section(_django_stack())

        assert "Django" in section
        assert "python manage.py runserver 0.0.0.0:3000" in section
        assert "pip install --break-system-packages -r requirements.txt" in section

    def test_has_start_and_end_markers(self):
        section = _render_section(_django_stack())

        assert section.startswith("<!-- marcus:dev-preview:start -->")
        assert section.rstrip().endswith("<!-- marcus:dev-preview:end -->")

    def test_omits_install_line_from_shell_block_when_no_install_cmd(self):
        stack = ProjectStack(language="go", framework="", install_cmd="", dev_cmd="air")
        section = _render_section(stack)

        # The bash block should contain exactly the dev command, no blank
        # leading install line.
        assert "```bash\nair\n```" in section


class TestApplySection:
    def test_creates_minimal_readme_when_none_exists(self):
        section = "<!-- marcus:dev-preview:start -->\nX\n<!-- marcus:dev-preview:end -->"

        result = _apply_section(None, section, "My App")

        assert result.startswith("# My App")
        assert section in result

    def test_appends_section_when_readme_has_no_marker_yet(self):
        existing = "# My App\n\nSome human-written notes.\n"
        section = "<!-- marcus:dev-preview:start -->\nX\n<!-- marcus:dev-preview:end -->"

        result = _apply_section(existing, section, "My App")

        assert "Some human-written notes." in result
        assert section in result

    def test_replaces_existing_marked_section_in_place(self):
        existing = (
            "# My App\n\nIntro text.\n\n"
            "<!-- marcus:dev-preview:start -->\nOLD CONTENT\n<!-- marcus:dev-preview:end -->\n\n"
            "## Other Section\nMore human text.\n"
        )
        new_section = "<!-- marcus:dev-preview:start -->\nNEW CONTENT\n<!-- marcus:dev-preview:end -->"

        result = _apply_section(existing, new_section, "My App")

        assert "OLD CONTENT" not in result
        assert "NEW CONTENT" in result
        assert "Intro text." in result
        assert "## Other Section" in result
        assert "More human text." in result

    def test_applying_the_same_section_twice_is_idempotent(self):
        section = "<!-- marcus:dev-preview:start -->\nX\n<!-- marcus:dev-preview:end -->"

        once = _apply_section(None, section, "My App")
        twice = _apply_section(once, section, "My App")

        assert once == twice


class TestUpdateDevPreviewReadmeSection:
    @pytest.mark.asyncio
    async def test_writes_and_pushes_the_section_on_first_run(self, tmp_path):
        bare, working = _init_bare_origin_with_working_clone(tmp_path)

        wrote = await update_dev_preview_readme_section(working, _django_stack())

        assert wrote is True
        pushed_readme = _readme_on_bare_main(bare)
        assert "Django" in pushed_readme
        assert "python manage.py runserver 0.0.0.0:3000" in pushed_readme
        assert "Managed by Marcus." in pushed_readme  # original content preserved

    @pytest.mark.asyncio
    async def test_second_call_with_same_stack_is_a_noop(self, tmp_path):
        bare, working = _init_bare_origin_with_working_clone(tmp_path)
        await update_dev_preview_readme_section(working, _django_stack())

        wrote_again = await update_dev_preview_readme_section(working, _django_stack())

        assert wrote_again is False

    @pytest.mark.asyncio
    async def test_changed_stack_updates_the_section_again(self, tmp_path):
        bare, working = _init_bare_origin_with_working_clone(tmp_path)
        await update_dev_preview_readme_section(working, _django_stack())

        flask_stack = ProjectStack(
            language="python", framework="Flask",
            install_cmd="pip install -r requirements.txt",
            dev_cmd="flask run --host 0.0.0.0 --port 3000",
        )
        wrote = await update_dev_preview_readme_section(working, flask_stack)

        assert wrote is True
        pushed_readme = _readme_on_bare_main(bare)
        assert "Flask" in pushed_readme
        assert "Django" not in pushed_readme

    @pytest.mark.asyncio
    async def test_never_touches_the_shared_working_clone(self, tmp_path):
        """This must operate through a throwaway clone, not the shared
        host working tree other Marcus flows depend on — see the
        module's docstring for why."""
        bare, working = _init_bare_origin_with_working_clone(tmp_path)
        before = (subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=working,
            check=True, capture_output=True, text=True,
        ).stdout)

        await update_dev_preview_readme_section(working, _django_stack())

        after = (subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=working,
            check=True, capture_output=True, text=True,
        ).stdout)
        assert before == after  # shared clone's own HEAD never moved
        assert "Django" not in Path(working, "README.md").read_text()

    @pytest.mark.asyncio
    async def test_nonexistent_repo_path_returns_false(self, tmp_path):
        result = await update_dev_preview_readme_section(
            str(tmp_path / "does-not-exist"), _django_stack()
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_origin_remote_returns_false_not_a_crash(self, tmp_path):
        working = tmp_path / "no-remote"
        working.mkdir()
        _run(["git", "init", "-b", "main"], cwd=str(working))

        result = await update_dev_preview_readme_section(str(working), _django_stack())

        assert result is False

    @pytest.mark.asyncio
    async def test_unreachable_remote_returns_false_not_a_crash(self, tmp_path):
        working = tmp_path / "bad-remote"
        working.mkdir()
        _run(["git", "init", "-b", "main"], cwd=str(working))
        _run(
            ["git", "remote", "add", "origin", "/nonexistent/path/to/nowhere.git"],
            cwd=str(working),
        )

        result = await update_dev_preview_readme_section(str(working), _django_stack())

        assert result is False
