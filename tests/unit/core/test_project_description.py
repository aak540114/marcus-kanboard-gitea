"""
Unit tests for src/core/project_description.py

Tests cover:
- parse_stack_from_text: language detection, framework detection, field extraction,
  dev-cmd inference, install-cmd inference, minimum-field validation
- ProjectDescriptionManager: read/write, seed_if_missing, get_stack
"""

import json
from datetime import datetime

import pytest
from pathlib import Path

from src.core.project_description import (
    MAX_HISTORY_ENTRIES,
    ProjectDescriptionInferrer,
    ProjectDescriptionManager,
    ProjectStack,
    SOURCE_ABSENT,
    SOURCE_AGENT,
    SOURCE_HUMAN,
    SOURCE_INFERRED,
    SOURCE_TEMPLATE,
    _TEMPLATE,
    _WAITING_COMMENT,
    compute_diff_lines,
    format_provenance_badge,
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

    # ── apk_packages property (Alpine names for the live dev-env image) ──

    def test_python_apk_packages(self):
        """python installs its runtime via apk (bare alpine base has none)."""
        stack = ProjectStack(language="python")
        assert "python3" in stack.apk_packages
        assert "py3-pip" in stack.apk_packages

    def test_nodejs_apk_packages(self):
        """nodejs stack installs nodejs + npm via apk."""
        stack = ProjectStack(language="nodejs")
        assert "nodejs" in stack.apk_packages
        assert "npm" in stack.apk_packages

    def test_go_apk_uses_alpine_name(self):
        """Go's Alpine package is 'go', not Debian's 'golang'."""
        stack = ProjectStack(language="go")
        assert stack.apk_packages == ["go"]

    def test_java_apk_uses_alpine_name(self):
        """Java's Alpine package is 'openjdk17', not Debian's 'default-jdk'."""
        stack = ProjectStack(language="java")
        assert "openjdk17" in stack.apk_packages

    def test_extra_apk_appended(self):
        """extra_apt packages are appended to the apk base list too."""
        stack = ProjectStack(language="nodejs", extra_apt=["imagemagick"])
        assert "imagemagick" in stack.apk_packages


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
# Provenance: ticket_id + timestamp tracking (get_provenance)
# ---------------------------------------------------------------------------


class TestGetProvenance:
    """get_provenance records who/what/when, on top of the plain source."""

    def _mgr(self, tmp_path):
        return ProjectDescriptionManager(data_dir=tmp_path)

    def test_absent_project_has_no_ticket_or_timestamp(self, tmp_path):
        mgr = self._mgr(tmp_path)
        info = mgr.get_provenance(7)
        assert info == {"source": SOURCE_ABSENT, "ticket_id": None, "updated_at": None}

    def test_agent_update_records_ticket_id_and_timestamp(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# X\n", source=SOURCE_AGENT, ticket_id="42")
        info = mgr.get_provenance(7)
        assert info["source"] == SOURCE_AGENT
        assert info["ticket_id"] == "42"
        assert info["updated_at"] is not None
        # Must be a real, parseable ISO-8601 timestamp.
        datetime.fromisoformat(info["updated_at"])

    def test_get_source_stays_a_thin_wrapper(self, tmp_path):
        """get_source keeps returning just the plain string as before."""
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# X\n", source=SOURCE_AGENT, ticket_id="42")
        assert mgr.get_source(7) == SOURCE_AGENT

    def test_human_edit_has_no_ticket_id(self, tmp_path):
        """Human edits (via the web UI) are never tied to a specific ticket."""
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# Human wrote this\n")  # default = human
        info = mgr.get_provenance(7)
        assert info["source"] == SOURCE_HUMAN
        assert info["ticket_id"] is None
        assert info["updated_at"] is not None

    def test_legacy_bare_string_sidecar_still_parses(self, tmp_path):
        """A sidecar written by the pre-JSON format still parses correctly."""
        mgr = self._mgr(tmp_path)
        (tmp_path / "9.md").write_text("# Legacy\n", encoding="utf-8")
        (tmp_path / "9.source").write_text("human", encoding="utf-8")
        info = mgr.get_provenance(9)
        assert info == {"source": SOURCE_HUMAN, "ticket_id": None, "updated_at": None}
        assert mgr.get_source(9) == SOURCE_HUMAN

    def test_second_update_replaces_ticket_id_and_timestamp(self, tmp_path):
        """A later update's provenance replaces the earlier one entirely."""
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# v1\n", source=SOURCE_AGENT, ticket_id="1")
        mgr.update_description(7, "# v2\n", source=SOURCE_AGENT, ticket_id="2")
        info = mgr.get_provenance(7)
        assert info["ticket_id"] == "2"

    def test_truncated_utf8_sidecar_does_not_raise(self, tmp_path):
        """
        Regression test: a sidecar truncated mid multi-byte UTF-8
        character (e.g. a crash between update_description's two
        separate writes, landing inside a non-ASCII ticket_id) raised
        UnicodeDecodeError, which is NOT an OSError and was not caught.
        get_provenance/get_source/can_auto_update must degrade to the
        template default instead of propagating the exception — one
        caller is the /project-description page route, which has no
        exception handling of its own and would 500 on this, blocking
        the exact page a human needs to fix a corrupted sidecar.
        """
        mgr = self._mgr(tmp_path)
        (tmp_path / "10.md").write_text("# doc\n", encoding="utf-8")
        with open(tmp_path / "10.source", "wb") as f:
            # A JSON prefix truncated mid-way through a multi-byte
            # UTF-8 character (0xC3 alone is an incomplete 2-byte
            # sequence) — invalid UTF-8, distinct from invalid JSON.
            f.write(b'{"source": "agent", "ticket_id": "caf\xc3')

        info = mgr.get_provenance(10)

        assert info == {"source": SOURCE_TEMPLATE, "ticket_id": None, "updated_at": None}
        assert mgr.get_source(10) == SOURCE_TEMPLATE
        assert mgr.can_auto_update(10) is True


# ---------------------------------------------------------------------------
# format_provenance_badge
# ---------------------------------------------------------------------------


class TestFormatProvenanceBadge:
    """Human-readable 'last updated by ...' text for the description page."""

    def test_agent_update_with_ticket_id(self):
        text = format_provenance_badge(
            {
                "source": SOURCE_AGENT,
                "ticket_id": "42",
                "updated_at": "2026-08-19T14:32:00+00:00",
            }
        )
        assert text == "Last updated by AI working ticket 42 at 2026-08-19 14:32 UTC"

    def test_agent_update_without_ticket_id_falls_back_gracefully(self):
        text = format_provenance_badge(
            {
                "source": SOURCE_AGENT,
                "ticket_id": None,
                "updated_at": "2026-08-19T14:32:00+00:00",
            }
        )
        assert text == "Last updated by an AI agent at 2026-08-19 14:32 UTC"

    def test_agent_update_without_timestamp_omits_at_clause(self):
        text = format_provenance_badge(
            {"source": SOURCE_AGENT, "ticket_id": "42", "updated_at": None}
        )
        assert text == "Last updated by AI working ticket 42"
        assert " at " not in text

    def test_inferred_update_mentions_marcus_and_ticket(self):
        text = format_provenance_badge(
            {
                "source": SOURCE_INFERRED,
                "ticket_id": "7",
                "updated_at": "2026-08-19T14:32:00+00:00",
            }
        )
        assert text is not None
        assert "Marcus" in text
        assert "ticket 7" in text

    def test_human_update(self):
        text = format_provenance_badge(
            {
                "source": SOURCE_HUMAN,
                "ticket_id": None,
                "updated_at": "2026-08-19T14:32:00+00:00",
            }
        )
        assert text == "Last updated by a human at 2026-08-19 14:32 UTC"

    def test_template_shows_placeholder_note_not_none_strings(self):
        text = format_provenance_badge(
            {"source": SOURCE_TEMPLATE, "ticket_id": None, "updated_at": None}
        )
        assert text is not None
        assert "None" not in text

    def test_absent_returns_none(self):
        assert (
            format_provenance_badge(
                {"source": SOURCE_ABSENT, "ticket_id": None, "updated_at": None}
            )
            is None
        )


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


# ---------------------------------------------------------------------------
# get_history: per-update audit trail, capped at MAX_HISTORY_ENTRIES
# ---------------------------------------------------------------------------


class TestGetHistory:
    """Every update_description() call appends a history entry."""

    def _mgr(self, tmp_path):
        return ProjectDescriptionManager(data_dir=tmp_path)

    def test_no_history_when_never_updated(self, tmp_path):
        mgr = self._mgr(tmp_path)
        assert mgr.get_history(7) == []

    def test_single_update_produces_one_entry(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# v1\n", source=SOURCE_AGENT, ticket_id="42")

        history = mgr.get_history(7)

        assert len(history) == 1
        assert history[0]["source"] == SOURCE_AGENT
        assert history[0]["ticket_id"] == "42"
        assert history[0]["text"] == "# v1\n"
        assert history[0]["updated_at"] is not None

    def test_returned_newest_first(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# v1\n", source=SOURCE_AGENT, ticket_id="1")
        mgr.update_description(7, "# v2\n", source=SOURCE_AGENT, ticket_id="2")
        mgr.update_description(7, "# v3\n", source=SOURCE_HUMAN)

        history = mgr.get_history(7)

        assert [h["text"] for h in history] == ["# v3\n", "# v2\n", "# v1\n"]

    def test_capped_at_max_history_entries(self, tmp_path):
        mgr = self._mgr(tmp_path)
        for i in range(MAX_HISTORY_ENTRIES + 5):
            mgr.update_description(
                7, f"# v{i}\n", source=SOURCE_AGENT, ticket_id=str(i)
            )

        history = mgr.get_history(7)

        assert len(history) == MAX_HISTORY_ENTRIES
        # Oldest entries (v0..v4) were dropped; newest-first starts at the
        # last update and the oldest surviving entry is v5.
        assert history[0]["text"] == f"# v{MAX_HISTORY_ENTRIES + 4}\n"
        assert history[-1]["text"] == "# v5\n"

    def test_limit_parameter_truncates_further(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.update_description(7, "# v1\n", source=SOURCE_AGENT, ticket_id="1")
        mgr.update_description(7, "# v2\n", source=SOURCE_AGENT, ticket_id="2")
        mgr.update_description(7, "# v3\n", source=SOURCE_HUMAN)

        history = mgr.get_history(7, limit=2)

        assert len(history) == 2
        assert history[0]["text"] == "# v3\n"

    def test_on_disk_history_file_itself_is_capped(self, tmp_path):
        """The stored history file must not grow past MAX_HISTORY_ENTRIES.

        get_history() always truncates its return value to `limit`, so a
        test asserting only on get_history()'s output would pass even if
        the on-disk file were never trimmed on write — the file would
        just grow unbounded forever. This reads the raw sidecar file to
        confirm the trim in _append_history is actually happening.
        """
        mgr = self._mgr(tmp_path)
        for i in range(MAX_HISTORY_ENTRIES + 5):
            mgr.update_description(
                7, f"# v{i}\n", source=SOURCE_AGENT, ticket_id=str(i)
            )

        raw = json.loads((tmp_path / "7.history.json").read_text(encoding="utf-8"))

        assert len(raw) == MAX_HISTORY_ENTRIES

    def test_history_is_per_project(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.update_description(1, "# project one\n", source=SOURCE_HUMAN)
        mgr.update_description(2, "# project two\n", source=SOURCE_HUMAN)

        assert len(mgr.get_history(1)) == 1
        assert mgr.get_history(1)[0]["text"] == "# project one\n"
        assert mgr.get_history(2)[0]["text"] == "# project two\n"

    def test_corrupted_history_file_degrades_to_empty(self, tmp_path):
        """A malformed .history.json must not crash get_history."""
        mgr = self._mgr(tmp_path)
        (tmp_path / "9.md").write_text("# doc\n", encoding="utf-8")
        (tmp_path / "9.history.json").write_text("not valid json{{{", encoding="utf-8")

        assert mgr.get_history(9) == []

    def test_history_write_failure_does_not_break_the_update(self, tmp_path, monkeypatch):
        """
        A history-write failure is best-effort: the description content
        and provenance must still be saved successfully.
        """
        mgr = self._mgr(tmp_path)

        real_write_text = Path.write_text

        def flaky_write_text(self, *args, **kwargs):
            if self.name.endswith(".history.json"):
                raise OSError("disk full")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", flaky_write_text)

        mgr.update_description(7, "# saved anyway\n", source=SOURCE_HUMAN)

        assert mgr.get_description(7) == "# saved anyway\n"
        assert mgr.get_source(7) == SOURCE_HUMAN
        # The history write genuinely failed — confirm it didn't silently
        # succeed some other way.
        assert mgr.get_history(7) == []


# ---------------------------------------------------------------------------
# compute_diff_lines
# ---------------------------------------------------------------------------


class TestComputeDiffLines:
    def test_added_lines_marked_add(self):
        diff = compute_diff_lines("", "line one\nline two\n")
        kinds = {kind for kind, _ in diff}
        assert kinds == {"add"}
        assert ("add", "line one") in diff
        assert ("add", "line two") in diff

    def test_removed_lines_marked_remove(self):
        diff = compute_diff_lines("line one\nline two\n", "")
        kinds = {kind for kind, _ in diff}
        assert kinds == {"remove"}

    def test_identical_text_produces_no_diff_lines(self):
        text = "same\ntext\n"
        assert compute_diff_lines(text, text) == []

    def test_single_line_change_shows_remove_and_add(self):
        diff = compute_diff_lines("Language: Python\n", "Language: Node.js\n")
        assert ("remove", "Language: Python") in diff
        assert ("add", "Language: Node.js") in diff

    def test_unchanged_surrounding_lines_marked_context(self):
        old = "line 1\nline 2\nline 3\n"
        new = "line 1\nCHANGED\nline 3\n"
        diff = compute_diff_lines(old, new)
        kinds = [kind for kind, _ in diff]
        assert "context" in kinds
        assert ("remove", "line 2") in diff
        assert ("add", "CHANGED") in diff
