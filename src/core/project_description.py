"""
Per-project description store and tech-stack parser.

Every Kanboard project has an associated markdown document stored at
``./data/project_descriptions/{project_id}.md``.  This document is the
single source of truth for:

- The tech stack (language, framework, packages, dev-server command)
- High-level project context that AI agents carry through all tickets

AI agents read this via the Marcus MCP tool ``get_project_description``
(``src/marcus_mcp/tools/human_gated.py``) — read-only for agents.  Humans
view and edit it through the Marcus web UI at
``/project-description?project_id={id}`` (backed by
``/api/project-description``, ``GET``/``PUT``, in ``server.py``).

Classes
-------
ProjectStack
    Parsed tech-stack information extracted from the description.
ProjectDescriptionManager
    Reads, writes, and parses project description documents.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path(os.getcwd()) / "data" / "project_descriptions"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_TEMPLATE = """\
# {name}

## Overview
<!-- Describe what this project does in 2-3 sentences. -->

## Tech Stack
<!-- Required: list the language and framework so Marcus can set up the dev environment. -->
- **Language**: <!-- e.g. Python, Node.js, Go, Rust, Ruby, Java, PHP -->
- **Framework**: <!-- e.g. FastAPI, Flask, Express, Gin, Rails -->
- **Database**: <!-- e.g. PostgreSQL, SQLite, MongoDB (or "none") -->
- **Dev server command**: <!-- e.g. uvicorn main:app --port 3000, npm run dev -->
- **Install command**: <!-- e.g. pip install -r requirements.txt, npm install -->

## Architecture Notes
<!-- High-level design decisions, key modules, API shape, etc. -->

## Open Questions
<!-- Things that need human input before AI agents can proceed. -->
"""

_WAITING_COMMENT = (
    "🤔 **Clarification needed before I can start work.**\n\n"
    "I could not find tech-stack information (language / framework / dev-server "
    "command) in this project's description.  Please:\n\n"
    "1. Open the **Project Description** page (button in the board header)\n"
    "2. Fill in the **Tech Stack** section — at minimum *Language* and "
    "*Dev server command*\n"
    "3. Move this ticket back to **Ready** once you have updated the description\n\n"
    "I will pick it up automatically after that."
)


@dataclass
class ProjectStack:
    """Tech-stack information extracted from the project description.

    Parameters
    ----------
    language : str
        Programming language, e.g. ``"python"``, ``"nodejs"``, ``"go"``.
    framework : str
        Web framework, e.g. ``"fastapi"``, ``"express"``.  Empty string if
        not specified.
    install_cmd : str
        Shell command to install dependencies inside the container, e.g.
        ``"pip install -r requirements.txt"``.
    dev_cmd : str
        Shell command to start the dev server on port 3000, e.g.
        ``"uvicorn main:app --host 0.0.0.0 --port 3000"``.
    use_hm_reload : bool
        ``True`` when the dev command has in-process hot-module replacement
        (currently only Node.js / Vite / webpack) and should NOT be wrapped
        with an inotifywait restart loop.
    extra_apt : List[str]
        Additional ``apt-get install`` packages needed for this stack.
    """

    language: str
    framework: str = ""
    install_cmd: str = ""
    dev_cmd: str = "python -m http.server 3000"
    use_hm_reload: bool = False
    extra_apt: List[str] = field(default_factory=list)

    @property
    def apt_packages(self) -> List[str]:
        """Base apt packages for the detected language + any extras."""
        base: List[str] = []
        lang = self.language.lower()
        if lang == "python":
            base = ["python3", "python3-pip", "python3-venv"]
        elif lang in ("nodejs", "node", "javascript", "typescript"):
            base = ["nodejs", "npm"]
        elif lang == "go":
            base = ["golang"]
        elif lang == "rust":
            base = ["rustc", "cargo"]
        elif lang == "ruby":
            base = ["ruby", "ruby-bundler"]
        elif lang in ("java", "kotlin"):
            base = ["default-jdk", "maven"]
        elif lang == "php":
            base = ["php-cli", "composer"]
        return base + self.extra_apt


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_stack_from_text(text: str) -> Optional[ProjectStack]:
    """Extract tech-stack details from a free-form markdown description.

    Parameters
    ----------
    text : str
        Raw markdown content of the project description.

    Returns
    -------
    Optional[ProjectStack]
        Parsed stack, or ``None`` if the minimum required fields (language and
        dev-server command) cannot be determined.
    """
    if not text:
        return None

    # Strip HTML comments before keyword matching so placeholder text in
    # templates (e.g. <!-- e.g. Node.js, Go, Rust ... -->) is invisible to
    # the language/framework detectors.
    clean = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    low = clean.lower()

    # ── Language ────────────────────────────────────────────────────────────
    language = ""
    if "node.js" in low or "nodejs" in low or "javascript" in low or "typescript" in low:
        language = "nodejs"
    elif "python" in low:
        language = "python"
    elif "rust" in low:
        # Check rust before go: "cargo run" contains "go " and would otherwise
        # be mis-detected as Go.
        language = "rust"
    elif "golang" in low or re.search(r"(?<![a-z])go(?![a-z])", low):
        language = "go"
    elif "ruby" in low or "rails" in low:
        language = "ruby"
    elif "java" in low or "kotlin" in low or "spring" in low:
        language = "java"
    elif "php" in low or "laravel" in low or "symfony" in low:
        language = "php"

    # ── Framework ────────────────────────────────────────────────────────────
    framework = ""
    _fw_map = {
        "fastapi": "fastapi",
        "flask": "flask",
        "django": "django",
        "express": "express",
        "next.js": "nextjs",
        "nextjs": "nextjs",
        "nuxt": "nuxt",
        "rails": "rails",
        "sinatra": "sinatra",
        "laravel": "laravel",
        "symfony": "symfony",
        "spring": "spring",
        "gin": "gin",
        "echo": "echo",
        "fiber": "fiber",
        "actix": "actix",
        "axum": "axum",
    }
    for keyword, name in _fw_map.items():
        if keyword in low:
            framework = name
            break

    # ── Explicit "Dev server command" field ────────────────────────────────
    dev_cmd = _extract_field(text, "dev server command") or _extract_field(
        text, "dev-server command"
    )

    # ── Explicit "Install command" field ──────────────────────────────────
    install_cmd = _extract_field(text, "install command")

    # ── Infer dev_cmd if not explicit ─────────────────────────────────────
    if not dev_cmd:
        if language == "python":
            if framework in ("fastapi",):
                dev_cmd = "uvicorn main:app --host 0.0.0.0 --port 3000"
            elif framework == "flask":
                dev_cmd = "flask run --host 0.0.0.0 --port 3000"
            elif framework == "django":
                dev_cmd = "python manage.py runserver 0.0.0.0:3000 --noreload"
            else:
                dev_cmd = "python -m http.server 3000"
        elif language == "nodejs":
            dev_cmd = "npm run dev -- --port 3000"
        elif language == "go":
            dev_cmd = "$(go env GOPATH)/bin/air"
        elif language == "rust":
            dev_cmd = "cargo watch -x run"
        elif language == "ruby":
            dev_cmd = "bundle exec ruby app.rb -p 3000"
        elif language == "java":
            dev_cmd = "mvn spring-boot:run -Dspring-boot.run.jvmArguments='-Dserver.port=3000'"
        elif language == "php":
            dev_cmd = "php -S 0.0.0.0:3000"

    # ── Infer install_cmd if not explicit ─────────────────────────────────
    if not install_cmd:
        if language == "python":
            install_cmd = "pip install --no-cache-dir -r requirements.txt 2>/dev/null || true"
        elif language == "nodejs":
            install_cmd = "npm install"
        elif language == "go":
            install_cmd = "go install github.com/air-verse/air@latest"
        elif language == "rust":
            install_cmd = "cargo install cargo-watch"
        elif language == "ruby":
            install_cmd = "bundle install 2>/dev/null || true"

    # ── Require minimum: language + dev command ────────────────────────────
    if not language or not dev_cmd:
        return None

    use_hm_reload = language == "nodejs"

    return ProjectStack(
        language=language,
        framework=framework,
        install_cmd=install_cmd,
        dev_cmd=dev_cmd,
        use_hm_reload=use_hm_reload,
    )


def _extract_field(text: str, field_name: str) -> str:
    """Pull the value after a markdown list item like ``- **Field Name**: value``.

    Parameters
    ----------
    text : str
        Markdown text to search.
    field_name : str
        Field label to look for (case-insensitive).

    Returns
    -------
    str
        Extracted value, stripped and without surrounding markdown comment
        markers.  Empty string if not found or if the value is a placeholder.
    """
    pattern = re.compile(
        r"[-*]\s*\*{0,2}" + re.escape(field_name) + r"\*{0,2}\s*:?\s*(.+)",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return ""
    value = m.group(1).strip()
    # Strip markdown comment markers and ignore placeholder text
    value = re.sub(r"<!--.*?-->", "", value).strip()
    if not value or value.startswith("<!--") or "e.g." in value.lower():
        return ""
    return value


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ProjectDescriptionManager:
    """Reads and writes per-project description documents.

    Documents are stored as markdown files at::

        <data_dir>/<project_id>.md

    Parameters
    ----------
    data_dir : Optional[Path]
        Override the default storage directory.  Defaults to
        ``./data/project_descriptions/`` relative to the Marcus working
        directory.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        """Initialise the manager."""
        self._dir = data_dir or _DEFAULT_DATA_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _path(self, project_id: int) -> Path:
        return self._dir / f"{project_id}.md"

    def get_description(self, project_id: int) -> Optional[str]:
        """Return the raw markdown description for a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[str]
            Markdown text, or ``None`` if the project has no description yet.
        """
        p = self._path(project_id)
        if not p.exists():
            return None
        try:
            return p.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read project description %s: %s", p, exc)
            return None

    def update_description(self, project_id: int, text: str) -> None:
        """Overwrite the description for a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        text : str
            New markdown content.
        """
        p = self._path(project_id)
        try:
            p.write_text(text, encoding="utf-8")
            logger.info("Updated project description for project %d", project_id)
        except OSError as exc:
            logger.error("Could not write project description %s: %s", p, exc)
            raise

    def seed_if_missing(self, project_id: int, project_name: str) -> None:
        """Create a blank description template if none exists yet.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        project_name : str
            Human-readable project name used in the template heading.
        """
        if not self._path(project_id).exists():
            self.update_description(
                project_id, _TEMPLATE.format(name=project_name)
            )
            logger.info(
                "Seeded blank description for project %d (%s)", project_id, project_name
            )

    def get_stack(self, project_id: int) -> Optional[ProjectStack]:
        """Parse and return the tech stack for a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[ProjectStack]
            Parsed stack, or ``None`` if the description is missing or
            does not contain enough tech-stack information.
        """
        text = self.get_description(project_id)
        if text is None:
            return None
        return parse_stack_from_text(text)
