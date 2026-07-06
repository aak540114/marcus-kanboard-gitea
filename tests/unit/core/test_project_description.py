"""
Unit tests for src/core/project_description.py

Tests cover:
- parse_stack_from_text: language detection, framework detection, field extraction,
  dev-cmd inference, install-cmd inference, minimum-field validation
- ProjectDescriptionManager: read/write, seed_if_missing, get_stack
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from src.core.project_description import (
    ProjectDescriptionManager,
    ProjectStack,
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
