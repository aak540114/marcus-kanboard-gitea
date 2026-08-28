"""
Unit tests for src/marcus_mcp/server.py's
_reconcile_stack_with_repo / _persist_corrected_stack / _declared_stack_key.

Regression coverage for two layers of the same bug. #1: a project's
description confidently declared "Python / Django" (an LLM's best-guess
inference from a single ticket, never checked against the real repo)
while the actual repo was a small Node.js/Express server — every
dev-environment preview 404'd because Marcus ran `python manage.py
runserver` against a repo with no manage.py at all. #2 (one layer down,
still reproducible after #1's fix): the description correctly says
"Python" but never specifically names Django, even though the repo
unmistakably IS a Django app (`manage.py` present) — comparing only the
LANGUAGE treated "python" == "python" as a match and never noticed the
framework was wrong, so the preview kept running the wrong dev command
(e.g. a generic `python -m http.server` instead of `python manage.py
runserver`) and kept 404ing despite the language being "correct".

These tests exercise the reconciliation logic directly against a real
ProjectDescriptionManager (temp-file backed, not mocked) and real
temporary repo directories, matching this codebase's established
"empirically verify against the real implementation" testing style for
anything involving file/text content.
"""

from pathlib import Path

from src.core.project_description import (
    ProjectDescriptionManager,
    ProjectStack,
    SOURCE_AGENT,
    SOURCE_HUMAN,
    SOURCE_INFERRED,
)
from src.marcus_mcp.server import (
    _declared_stack_key,
    _persist_corrected_stack,
    _reconcile_stack_with_repo,
)


def _mgr(tmp_path: Path) -> ProjectDescriptionManager:
    return ProjectDescriptionManager(data_dir=tmp_path / "descriptions")


def _django_declared_stack() -> ProjectStack:
    return ProjectStack(
        language="python",
        framework="Django",
        install_cmd="pip install --break-system-packages -r requirements.txt",
        dev_cmd="python manage.py runserver 0.0.0.0:3000",
        use_hm_reload=False,
    )


class TestReconcileStackWithRepo:
    def test_no_repo_path_returns_stack_unchanged(self, tmp_path):
        mgr = _mgr(tmp_path)
        declared = _django_declared_stack()

        result = _reconcile_stack_with_repo(mgr, 7, declared, None)

        assert result is declared

    def test_nonexistent_repo_path_returns_stack_unchanged(self, tmp_path):
        mgr = _mgr(tmp_path)
        declared = _django_declared_stack()

        result = _reconcile_stack_with_repo(
            mgr, 7, declared, str(tmp_path / "does-not-exist")
        )

        assert result is declared

    def test_matching_language_returns_stack_unchanged(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "app"}')
        mgr = _mgr(tmp_path)
        declared = ProjectStack(
            language="nodejs", framework="Express",
            install_cmd="npm install", dev_cmd="npm start",
        )

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert result is declared

    def test_empty_repo_is_too_weak_a_signal_to_override(self, tmp_path):
        """No recognized manifest file at all (detect_project_type ->
        "static") must not override an already-declared stack — an
        early/empty repo is not evidence the declared stack is wrong."""
        repo = tmp_path / "repo"
        repo.mkdir()
        mgr = _mgr(tmp_path)
        declared = _django_declared_stack()

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert result is declared

    def test_mismatch_returns_corrected_nodejs_stack(self, tmp_path):
        """The exact reported bug: description says Python/Django, repo
        is actually Node.js."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "app"}')
        mgr = _mgr(tmp_path)
        declared = _django_declared_stack()

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert result is not declared
        assert result.language == "nodejs"
        assert result.install_cmd == "npm install"
        assert "npm run dev" in result.dev_cmd

    def test_django_detection_gets_a_framework_label(self, tmp_path):
        """Mismatch in the other direction — declared nodejs, repo is
        actually Django — confirms the framework label mapping."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("Django>=4.2\n")
        (repo / "manage.py").write_text("#!/usr/bin/env python\n")
        mgr = _mgr(tmp_path)
        declared = ProjectStack(
            language="nodejs", framework="", install_cmd="npm install",
            dev_cmd="npm start",
        )

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert result.language == "python"
        assert result.framework == "Django"
        assert "manage.py" in result.dev_cmd
        assert "--break-system-packages" in result.install_cmd

    def test_mismatch_persists_corrected_description_when_auto_updatable(
        self, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "app"}')
        mgr = _mgr(tmp_path)
        mgr.update_description(
            7,
            "# Demo\n\n## Overview\nA CMS.\n\n## Tech Stack\n"
            "- **Language**: Python\n- **Framework**: Django\n"
            "- **Database**: SQLite\n"
            "- **Dev server command**: python manage.py runserver 0.0.0.0:3000\n"
            "- **Install command**: pip install -r requirements.txt\n\n"
            "## Architecture Notes\nFabricated details.\n\n"
            "## Open Questions\nNone.\n",
            source=SOURCE_INFERRED,
        )
        declared = _django_declared_stack()

        _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        text = mgr.get_description(7)
        assert "Node.js" in text
        assert "npm install" in text
        assert "Django" not in text
        # Other sections must survive untouched.
        assert "A CMS." in text
        assert "Fabricated details." in text
        assert "None." in text
        assert mgr.get_source(7) == SOURCE_INFERRED

    def test_mismatch_does_not_persist_over_a_human_edit(self, tmp_path):
        """A human's explicit description always wins — Marcus may still
        use the correct stack for THIS preview, but must never silently
        overwrite what a human wrote."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "app"}')
        mgr = _mgr(tmp_path)
        original_text = (
            "# Demo\n\n## Tech Stack\n- **Language**: Python\n"
            "- **Framework**: Django\n"
            "- **Dev server command**: python manage.py runserver 0.0.0.0:3000\n"
            "- **Install command**: pip install -r requirements.txt\n"
        )
        mgr.update_description(7, original_text, source=SOURCE_HUMAN)
        declared = _django_declared_stack()

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        # Stored text is untouched.
        assert mgr.get_description(7) == original_text
        assert mgr.get_source(7) == SOURCE_HUMAN
        # But the corrected stack is still used for THIS preview attempt.
        assert result.language == "nodejs"

    def test_agent_authored_description_is_still_correctable(self, tmp_path):
        """SOURCE_AGENT (not just SOURCE_INFERRED) is also AI content —
        equally correctable, matching can_auto_update's own definition of
        "not yet human-locked"."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "app"}')
        mgr = _mgr(tmp_path)
        mgr.update_description(
            7,
            "## Tech Stack\n- **Language**: Python\n"
            "- **Dev server command**: python manage.py runserver 0.0.0.0:3000\n"
            "- **Install command**: pip install -r requirements.txt\n",
            source=SOURCE_AGENT,
            ticket_id="42",
        )
        declared = _django_declared_stack()

        _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert "Node.js" in mgr.get_description(7)


class TestPersistCorrectedStack:
    def test_no_description_yet_is_a_safe_no_op(self, tmp_path):
        mgr = _mgr(tmp_path)
        corrected = ProjectStack(
            language="nodejs", framework="", install_cmd="npm install",
            dev_cmd="npm start",
        )

        _persist_corrected_stack(mgr, 99, corrected)  # must not raise

        assert mgr.get_description(99) is None

    def test_unrecognized_format_is_a_safe_no_op(self, tmp_path):
        mgr = _mgr(tmp_path)
        freeform = "Just some free-form human notes with no headers.\n"
        mgr.update_description(7, freeform, source=SOURCE_HUMAN)
        corrected = ProjectStack(
            language="nodejs", framework="", install_cmd="npm install",
            dev_cmd="npm start",
        )

        _persist_corrected_stack(mgr, 7, corrected)

        assert mgr.get_description(7) == freeform

    def test_preserves_existing_database_field(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.update_description(
            7,
            "## Tech Stack\n- **Language**: Python\n"
            "- **Database**: PostgreSQL\n"
            "- **Dev server command**: python manage.py runserver 0.0.0.0:3000\n"
            "- **Install command**: pip install -r requirements.txt\n",
            source=SOURCE_INFERRED,
        )
        corrected = ProjectStack(
            language="nodejs", framework="", install_cmd="npm install",
            dev_cmd="npm start",
        )

        _persist_corrected_stack(mgr, 7, corrected)

        assert "PostgreSQL" in mgr.get_description(7)

    def test_identical_content_does_not_rewrite_or_grow_history(self, tmp_path):
        """Regression: update_description() unconditionally appends a new
        entry to the bounded (20-entry) history log on every call, with
        no dedup check of its own. _maybe_ai_infer_stack re-persists its
        CACHED stack on every single preview start (not just when it
        actually changes) — for a project whose repo is permanently
        "static" to file-sniffing, that means every preview-start click
        would otherwise add a duplicate history entry, eventually
        pushing out genuinely meaningful history. Persisting the exact
        same stack twice must be a no-op the second time."""
        mgr = _mgr(tmp_path)
        mgr.update_description(
            7,
            "## Tech Stack\n- **Language**: Python\n"
            "- **Framework**: Django\n"
            "- **Database**: none\n"
            "- **Dev server command**: python manage.py runserver 0.0.0.0:3000\n"
            "- **Install command**: pip install -r requirements.txt\n",
            source=SOURCE_INFERRED,
        )
        before_history_len = len(mgr.get_history(7))
        stack = ProjectStack(
            language="python", framework="Django",
            install_cmd="pip install -r requirements.txt",
            dev_cmd="python manage.py runserver 0.0.0.0:3000",
        )

        _persist_corrected_stack(mgr, 7, stack)
        _persist_corrected_stack(mgr, 7, stack)
        _persist_corrected_stack(mgr, 7, stack)

        assert len(mgr.get_history(7)) == before_history_len


class TestDeclaredStackKey:
    def test_maps_django_framework_to_python_django(self):
        stack = ProjectStack(language="python", framework="Django")
        assert _declared_stack_key(stack) == "python-django"

    def test_maps_flask_framework_to_python_flask(self):
        stack = ProjectStack(language="python", framework="Flask")
        assert _declared_stack_key(stack) == "python-flask"

    def test_maps_fastapi_framework_to_python_fastapi(self):
        stack = ProjectStack(language="python", framework="FastAPI")
        assert _declared_stack_key(stack) == "python-fastapi"

    def test_is_case_insensitive(self):
        stack = ProjectStack(language="Python", framework="django")
        assert _declared_stack_key(stack) == "python-django"

    def test_bare_python_with_no_framework_named(self):
        stack = ProjectStack(language="python", framework="")
        assert _declared_stack_key(stack) == "python"

    def test_bare_python_with_unrecognized_framework_named(self):
        stack = ProjectStack(language="python", framework="Tornado")
        assert _declared_stack_key(stack) == "python"

    def test_non_python_language_returns_language_unchanged(self):
        stack = ProjectStack(language="nodejs", framework="Express")
        assert _declared_stack_key(stack) == "nodejs"


class TestReconcileStackSameLanguageSubtypeMismatch:
    """The exact gap the user re-reported: a Python repo whose declared
    framework doesn't (correctly) say Django, even though manage.py
    proves it is one."""

    def test_declared_python_no_framework_but_repo_is_django(self, tmp_path):
        """description says just "Python" (framework left blank) — the
        common case for an LLM's under-specified best-guess inference —
        but the repo has manage.py, so it unmistakably IS Django."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("Django>=4.2\n")
        (repo / "manage.py").write_text("#!/usr/bin/env python\n")
        mgr = _mgr(tmp_path)
        declared = ProjectStack(
            language="python", framework="",
            install_cmd="pip install -r requirements.txt",
            dev_cmd="python -m http.server 3000",
        )

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert result is not declared
        assert result.language == "python"
        assert result.framework == "Django"
        assert "manage.py" in result.dev_cmd

    def test_declared_python_wrong_framework_but_repo_is_django(self, tmp_path):
        """description names the wrong Python framework (Flask) while
        the repo is actually Django."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("Django>=4.2\n")
        (repo / "manage.py").write_text("#!/usr/bin/env python\n")
        mgr = _mgr(tmp_path)
        declared = ProjectStack(
            language="python", framework="Flask",
            install_cmd="pip install -r requirements.txt",
            dev_cmd="flask run --host 0.0.0.0 --port 3000",
        )

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert result.framework == "Django"
        assert "manage.py" in result.dev_cmd

    def test_declared_django_but_generic_python_detected_is_left_alone(
        self, tmp_path
    ):
        """The inverse: declared framework IS already Django, but the
        repo only weakly matches (no manage.py, no framework keyword in
        requirements.txt) — too weak a signal to downgrade an explicit
        declaration, matching the "static is too weak" precedent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("some-other-lib==1.0\n")
        mgr = _mgr(tmp_path)
        declared = _django_declared_stack()

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert result is declared

    def test_matching_subtype_returns_stack_unchanged(self, tmp_path):
        """Declared and detected both say Django — genuinely nothing to
        correct, so the object identity is preserved (no wasted rewrite)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("Django>=4.2\n")
        (repo / "manage.py").write_text("#!/usr/bin/env python\n")
        mgr = _mgr(tmp_path)
        declared = _django_declared_stack()

        result = _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        assert result is declared

    def test_subtype_mismatch_persists_corrected_description(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("Django>=4.2\n")
        (repo / "manage.py").write_text("#!/usr/bin/env python\n")
        mgr = _mgr(tmp_path)
        mgr.update_description(
            7,
            "# Demo\n\n## Tech Stack\n- **Language**: Python\n"
            "- **Dev server command**: python -m http.server 3000\n"
            "- **Install command**: pip install -r requirements.txt\n",
            source=SOURCE_INFERRED,
        )
        declared = ProjectStack(
            language="python", framework="",
            install_cmd="pip install -r requirements.txt",
            dev_cmd="python -m http.server 3000",
        )

        _reconcile_stack_with_repo(mgr, 7, declared, str(repo))

        text = mgr.get_description(7)
        assert "Django" in text
        assert "manage.py" in text
