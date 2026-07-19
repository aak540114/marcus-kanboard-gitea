"""
Unit tests for src/core/project_description.py

Tests cover:
- parse_stack_from_text: language detection, framework detection, field extraction,
  dev-cmd inference, install-cmd inference, minimum-field validation
- ProjectDescriptionManager: read/write, seed_if_missing, get_stack
"""

import pytest
from pathlib import Path

from src.core.project_description import (
    ProjectDescriptionInferrer,
    ProjectDescriptionManager,
    ProjectStack,
    SOURCE_ABSENT,
    SOURCE_HUMAN,
    SOURCE_INFERRED,
    SOURCE_TEMPLATE,
    _TEMPLATE,
    _WAITING_COMMENT,
    parse_stack_from_text,
)


# ---------------------------------------------------------------------------
# parse_stack_from_text
# ---------------------------------------------------------------------------


class TestParseStackFromText:
    """Tests for the free-form description parser."""

    # ── Language detection ──────────────────────────────────────────────

    def test_detects_python(self):
        """'Python' keyword → language python."""
        stack = parse_stack_from_text("Language: Python\nDev server command: uvicorn main:app")
        assert stack is not None
        assert stack.language == "python"

    def test_detects_nodejs_via_nodejs(self):
        """'nodejs' keyword → language nodejs."""
        stack = parse_stack_from_text("Language: nodejs\nDev server command: npm run dev")
        assert stack is not None
        assert stack.language == "nodejs"

    def test_detects_nodejs_via_javascript(self):
        """'javascript' keyword maps to nodejs."""
        stack = parse_stack_from_text("Language: javascript\n- Dev server command: node index.js")
        assert stack is not None
        assert stack.language == "nodejs"

    def test_detects_nodejs_via_typescript(self):
        """'typescript' keyword maps to nodejs."""
        stack = parse_stack_from_text("Language: typescript\n- Dev server command: ts-node src/index.ts")
        assert stack is not None
        assert stack.language == "nodejs"

    def test_detects_go(self):
        """'golang' keyword → language go."""
        stack = parse_stack_from_text("Language: golang\nDev server command: go run .")
        assert stack is not None
        assert stack.language == "go"

    def test_detects_rust(self):
        """'rust' keyword → language rust."""
        stack = parse_stack_from_text("Language: Rust\nDev server command: cargo run")
        assert stack is not None
        assert stack.language == "rust"

    def test_detects_ruby_via_rails(self):
        """'rails' keyword → language ruby."""
        stack = parse_stack_from_text("Framework: Rails\nDev server command: rails server -p 3000")
        assert stack is not None
        assert stack.language == "ruby"

    def test_detects_java(self):
        """'java' keyword → language java."""
        stack = parse_stack_from_text("Language: Java\nDev server command: mvn spring-boot:run")
        assert stack is not None
        assert stack.language == "java"

    def test_detects_php(self):
        """'php' keyword → language php."""
        stack = parse_stack_from_text("Language: PHP\nDev server command: php -S 0.0.0.0:3000")
        assert stack is not None
        assert stack.language == "php"

    def test_returns_none_when_no_language(self):
        """Returns None when no language and no dev command can be inferred."""
        assert parse_stack_from_text("Some vague description.") is None

    def test_returns_none_on_empty_string(self):
        """Empty text → None."""
        assert parse_stack_from_text("") is None

    # ── Framework detection ─────────────────────────────────────────────

    def test_detects_fastapi_framework(self):
        """'fastapi' keyword → framework fastapi."""
        stack = parse_stack_from_text("Language: Python\nFramework: FastAPI\nDev server command: uvicorn main:app")
        assert stack is not None
        assert stack.framework == "fastapi"

    def test_detects_flask_framework(self):
        """'flask' keyword → framework flask."""
        stack = parse_stack_from_text("Language: Python\nFramework: Flask\nDev server command: flask run")
        assert stack is not None
        assert stack.framework == "flask"

    def test_detects_django_framework(self):
        """'django' keyword → framework django."""
        stack = parse_stack_from_text("Language: Python\nFramework: Django\nDev server command: python manage.py runserver")
        assert stack is not None
        assert stack.framework == "django"

    def test_detects_express_framework(self):
        """'express' keyword → framework express."""
        stack = parse_stack_from_text("Language: nodejs\nFramework: Express\nDev server command: node app.js")
        assert stack is not None
        assert stack.framework == "express"

    def test_no_framework_when_absent(self):
        """Empty framework when not mentioned."""
        stack = parse_stack_from_text("Language: Python\nDev server command: python main.py")
        assert stack is not None
        assert stack.framework == ""

    # ── Explicit field extraction ───────────────────────────────────────

    def test_extracts_explicit_dev_command(self):
        """Explicit 'Dev server command' field is used verbatim."""
        stack = parse_stack_from_text(
            "- **Language**: Python\n"
            "- **Dev server command**: uvicorn app:app --host 0.0.0.0 --port 3000"
        )
        assert stack is not None
        assert stack.dev_cmd == "uvicorn app:app --host 0.0.0.0 --port 3000"

    def test_extracts_explicit_install_command(self):
        """Explicit 'Install command' field is used verbatim."""
        stack = parse_stack_from_text(
            "- **Language**: Python\n"
            "- **Install command**: pip install -r requirements.txt\n"
            "- **Dev server command**: python main.py"
        )
        assert stack is not None
        assert stack.install_cmd == "pip install -r requirements.txt"

    def test_ignores_placeholder_dev_command(self):
        """Template placeholder (e.g. ...) is not treated as a real value."""
        stack = parse_stack_from_text(
            "- **Language**: Python\n"
            "- **Dev server command**: <!-- e.g. uvicorn main:app --port 3000 -->"
        )
        # Placeholder stripped → falls through to inferred command
        assert stack is not None
        assert "e.g." not in stack.dev_cmd

    # ── Inferred commands ───────────────────────────────────────────────

    def test_infers_fastapi_dev_cmd(self):
        """python + fastapi → uvicorn inferred when no explicit command."""
        stack = parse_stack_from_text("Language: Python\nFramework: fastapi")
        assert stack is not None
        assert "uvicorn" in stack.dev_cmd

    def test_infers_flask_dev_cmd(self):
        """python + flask → flask run inferred."""
        stack = parse_stack_from_text("Language: Python\nFramework: flask")
        assert stack is not None
        assert "flask run" in stack.dev_cmd

    def test_infers_nodejs_dev_cmd(self):
        """nodejs → npm run dev inferred."""
        stack = parse_stack_from_text("Language: nodejs")
        assert stack is not None
        assert "npm run dev" in stack.dev_cmd

    def test_infers_python_install_cmd(self):
        """python → pip install inferred when not explicit."""
        stack = parse_stack_from_text("Language: Python\nDev server command: python main.py")
        assert stack is not None
        assert "pip install" in stack.install_cmd

    def test_infers_nodejs_install_cmd(self):
        """nodejs → npm install inferred when not explicit."""
        stack = parse_stack_from_text("Language: nodejs")
        assert stack is not None
        assert stack.install_cmd == "npm install"

    # ── HMR flag ────────────────────────────────────────────────────────

    def test_nodejs_sets_use_hm_reload_true(self):
        """nodejs stack uses native HMR (no inotifywait wrapper needed)."""
        stack = parse_stack_from_text("Language: nodejs")
        assert stack is not None
        assert stack.use_hm_reload is True

    def test_python_sets_use_hm_reload_false(self):
        """python stack does not use native HMR."""
        stack = parse_stack_from_text("Language: Python\nDev server command: uvicorn main:app")
        assert stack is not None
        assert stack.use_hm_reload is False

    # ── apt_packages property ────────────────────────────────────────────

    def test_python_apt_packages(self):
        """python stack includes python3, pip, venv."""
        stack = ProjectStack(language="python")
        pkgs = stack.apt_packages
        assert "python3" in pkgs
        assert "python3-pip" in pkgs

    def test_nodejs_apt_packages(self):
        """nodejs stack includes nodejs and npm."""
        stack = ProjectStack(language="nodejs")
        pkgs = stack.apt_packages
        assert "nodejs" in pkgs
        assert "npm" in pkgs

    def test_extra_apt_appended(self):
        """extra_apt packages are appended to base packages."""
        stack = ProjectStack(language="python", extra_apt=["libpq-dev", "redis-tools"])
        pkgs = stack.apt_packages
        assert "libpq-dev" in pkgs
        assert "redis-tools" in pkgs

    def test_unknown_language_gives_extra_only(self):
        """Unknown language returns only extra_apt packages."""
        stack = ProjectStack(language="cobol", extra_apt=["some-pkg"])
        assert stack.apt_packages == ["some-pkg"]


# ---------------------------------------------------------------------------
# ProjectDescriptionManager
# ---------------------------------------------------------------------------


class TestProjectDescriptionManager:
    """Tests for ProjectDescriptionManager read/write/seed operations."""

    @pytest.fixture()
    def mgr(self, tmp_path: Path) -> ProjectDescriptionManager:
        """Manager with a temp directory as storage."""
        return ProjectDescriptionManager(data_dir=tmp_path)

    def test_get_description_returns_none_when_missing(self, mgr):
        """Returns None for a project with no description file."""
        assert mgr.get_description(99) is None

    def test_update_and_get_roundtrip(self, mgr):
        """update_description then get_description returns the same text."""
        mgr.update_description(1, "# Hello\n\nSome markdown.")
        assert mgr.get_description(1) == "# Hello\n\nSome markdown."

    def test_update_overwrites_existing(self, mgr):
        """Second update_description call replaces the previous content."""
        mgr.update_description(1, "first")
        mgr.update_description(1, "second")
        assert mgr.get_description(1) == "second"

    def test_seed_if_missing_creates_file(self, mgr):
        """seed_if_missing writes a template file when none exists."""
        mgr.seed_if_missing(2, "My App")
        content = mgr.get_description(2)
        assert content is not None
        assert "My App" in content

    def test_seed_if_missing_does_not_overwrite(self, mgr):
        """seed_if_missing leaves an existing file unchanged."""
        mgr.update_description(2, "custom content")
        mgr.seed_if_missing(2, "My App")
        assert mgr.get_description(2) == "custom content"

    def test_get_stack_returns_none_when_no_file(self, mgr):
        """get_stack returns None when no description exists."""
        assert mgr.get_stack(5) is None

    def test_get_stack_returns_stack_when_parseable(self, mgr):
        """get_stack parses and returns a ProjectStack from a valid description."""
        mgr.update_description(
            3,
            "- **Language**: Python\n"
            "- **Framework**: FastAPI\n"
            "- **Dev server command**: uvicorn main:app --host 0.0.0.0 --port 3000\n",
        )
        stack = mgr.get_stack(3)
        assert stack is not None
        assert stack.language == "python"
        assert stack.framework == "fastapi"

    def test_get_stack_returns_none_for_blank_template(self, mgr):
        """A freshly-seeded blank template has no usable stack info."""
        mgr.seed_if_missing(4, "Blank Project")
        # Blank template has only placeholders → parse returns None
        assert mgr.get_stack(4) is None

    def test_files_isolated_per_project(self, mgr):
        """Each project_id has its own independent file."""
        mgr.update_description(10, "project ten")
        mgr.update_description(11, "project eleven")
        assert mgr.get_description(10) == "project ten"
        assert mgr.get_description(11) == "project eleven"


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    """Basic checks on module-level constants."""

    def test_waiting_comment_is_non_empty(self):
        """_WAITING_COMMENT exists and is a non-empty string."""
        assert isinstance(_WAITING_COMMENT, str)
        assert len(_WAITING_COMMENT) > 0

    def test_template_contains_tech_stack_section(self):
        """_TEMPLATE contains a Tech Stack section."""
        assert "Tech Stack" in _TEMPLATE

    def test_template_contains_language_placeholder(self):
        """_TEMPLATE has a Language placeholder for the human to fill in."""
        assert "Language" in _TEMPLATE


# ---------------------------------------------------------------------------
# Provenance: human edits lock out automated overwrites
# ---------------------------------------------------------------------------


class TestProvenance:
    """get_source / can_auto_update gate automated description writes."""

    def _mgr(self, tmp_path):
        return ProjectDescriptionManager(data_dir=tmp_path)

    def test_absent_when_no_description(self, tmp_path):
        """No file yet → SOURCE_ABSENT and auto-updatable."""
        mgr = self._mgr(tmp_path)
        assert mgr.get_source(7) == SOURCE_ABSENT
        assert mgr.can_auto_update(7) is True

    def test_seed_marks_template_and_stays_auto_updatable(self, tmp_path):
        """A seeded blank template is still auto-updatable."""
        mgr = self._mgr(tmp_path)
        mgr.seed_if_missing(7, "My Project")
        assert mgr.get_source(7) == SOURCE_TEMPLATE
        assert mgr.can_auto_update(7) is True

    def test_inferred_write_is_auto_updatable(self, tmp_path):
        """An inferred description can be refined again by automation."""
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# X\n", source=SOURCE_INFERRED)
        assert mgr.get_source(7) == SOURCE_INFERRED
        assert mgr.can_auto_update(7) is True

    def test_human_edit_locks_out_automation(self, tmp_path):
        """A human edit (default source) blocks further auto-updates."""
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# Human wrote this\n")  # default = human
        assert mgr.get_source(7) == SOURCE_HUMAN
        assert mgr.can_auto_update(7) is False

    def test_legacy_file_without_sidecar_treated_as_template(self, tmp_path):
        """A description written before provenance existed is auto-updatable."""
        mgr = self._mgr(tmp_path)
        # Simulate a legacy file: write the .md directly, no .source sidecar.
        (tmp_path / "9.md").write_text("# Legacy\n", encoding="utf-8")
        assert mgr.get_source(9) == SOURCE_TEMPLATE
        assert mgr.can_auto_update(9) is True


# ---------------------------------------------------------------------------
# ProjectDescriptionInferrer
# ---------------------------------------------------------------------------


class TestProjectDescriptionInferrer:
    """Infers a description from a ticket, LLM-first with heuristic fallback."""

    @pytest.mark.asyncio
    async def test_uses_llm_output_when_parseable(self):
        """A usable LLM description (has a stack) is returned verbatim."""
        llm_out = (
            "# Shop\n\n## Tech Stack\n- **Language**: Python\n"
            "- **Dev server command**: uvicorn main:app --port 3000\n"
        )

        async def fake_llm(prompt):
            return llm_out

        inf = ProjectDescriptionInferrer(llm_generate=fake_llm)
        result = await inf.infer("Shop", "Add checkout API", "FastAPI endpoint")
        assert result == llm_out.strip()

    @pytest.mark.asyncio
    async def test_falls_back_to_heuristic_when_llm_fails(self):
        """LLM error → keyword heuristic fills the template from ticket text."""

        async def boom(prompt):
            raise RuntimeError("model down")

        inf = ProjectDescriptionInferrer(llm_generate=boom)
        result = await inf.infer(
            "Shop", "Build a Python FastAPI service", "expose /orders"
        )
        assert result is not None
        assert parse_stack_from_text(result) is not None  # has a usable stack
        assert "Python" in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_language_detectable(self):
        """No LLM and no detectable language → None (caller asks the human)."""
        inf = ProjectDescriptionInferrer(llm_generate=None)
        result = await inf.infer("Shop", "Make it nicer", "look prettier")
        assert result is None
