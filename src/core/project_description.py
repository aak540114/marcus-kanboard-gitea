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
from typing import Awaitable, Callable, List, Optional

#: Provenance values stamped alongside a description (see
#: ProjectDescriptionManager.get_source). Only ``"human"`` locks a
#: description against automated (Marcus/agent) overwrites.
SOURCE_ABSENT = "absent"
SOURCE_TEMPLATE = "template"
SOURCE_INFERRED = "inferred"
SOURCE_AGENT = "agent"
SOURCE_HUMAN = "human"

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

    def _source_path(self, project_id: int) -> Path:
        return self._dir / f"{project_id}.source"

    def get_source(self, project_id: int) -> str:
        """Return who last wrote a project's description.

        One of :data:`SOURCE_ABSENT` (no description yet),
        :data:`SOURCE_TEMPLATE` (blank seed), :data:`SOURCE_INFERRED`
        (Marcus), :data:`SOURCE_AGENT` (a coding agent), or
        :data:`SOURCE_HUMAN`. A description file with no provenance sidecar
        (written before this feature existed) is assumed to be a
        ``template`` — automated inference only ever replaces a description
        with NO parseable stack, so a genuinely human-authored legacy
        description with a real stack is never at risk.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        str
            The provenance marker.
        """
        if not self._path(project_id).exists():
            return SOURCE_ABSENT
        sp = self._source_path(project_id)
        if sp.exists():
            try:
                val = sp.read_text(encoding="utf-8").strip()
                if val:
                    return val
            except OSError:
                pass
        return SOURCE_TEMPLATE

    def can_auto_update(self, project_id: int) -> bool:
        """Return ``True`` unless a human has edited this description.

        Marcus and agents may (re)write an absent, templated, or
        previously auto-generated description, but must NOT overwrite one a
        human corrected — that correction is authoritative.
        """
        return self.get_source(project_id) != SOURCE_HUMAN

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

    def update_description(
        self, project_id: int, text: str, source: str = SOURCE_HUMAN
    ) -> None:
        """Overwrite the description for a project and stamp its provenance.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        text : str
            New markdown content.
        source : str
            Who is writing — one of the ``SOURCE_*`` constants. Defaults to
            :data:`SOURCE_HUMAN` so any caller that does not opt out (e.g.
            the human edit route) locks the description against later
            automated overwrites. Automated callers pass
            :data:`SOURCE_INFERRED` / :data:`SOURCE_AGENT` /
            :data:`SOURCE_TEMPLATE`.
        """
        p = self._path(project_id)
        try:
            p.write_text(text, encoding="utf-8")
            self._source_path(project_id).write_text(source, encoding="utf-8")
            logger.info(
                "Updated project description for project %d (source=%s)",
                project_id,
                source,
            )
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
                project_id,
                _TEMPLATE.format(name=project_name),
                source=SOURCE_TEMPLATE,
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


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def _build_inference_prompt(
    project_name: str,
    ticket_title: str,
    ticket_description: str,
    acceptance_criteria: str,
) -> str:
    """Build the LLM prompt that infers a project description from a ticket."""
    return (
        "You are documenting a software project so that autonomous coding "
        "agents can set up a dev environment and work on it.\n\n"
        f"Project name: {project_name}\n\n"
        "You are given ONE ticket from this project. Infer the project's most "
        "likely tech stack and a short overview from it. Prefer widely-used "
        "defaults when the ticket is ambiguous (e.g. a web API → Python + "
        "FastAPI, or Node.js + Express). It is fine to be a best guess — a "
        "human will correct it.\n\n"
        f"Ticket title: {ticket_title}\n"
        f"Ticket description:\n{ticket_description}\n\n"
        f"Acceptance criteria:\n{acceptance_criteria}\n\n"
        "Respond with ONLY a markdown document in EXACTLY this structure "
        "(fill every field; the Language and Dev server command lines are "
        "REQUIRED):\n\n"
        f"# {project_name}\n\n"
        "## Overview\n<2-3 sentence plain-English description>\n\n"
        "## Tech Stack\n"
        "- **Language**: <one of: Python, Node.js, Go, Rust, Ruby, Java, PHP>\n"
        "- **Framework**: <e.g. FastAPI, Express, Gin, Rails, or 'none'>\n"
        "- **Database**: <e.g. PostgreSQL, SQLite, or 'none'>\n"
        "- **Dev server command**: <command that starts the app on port 3000>\n"
        "- **Install command**: <command that installs dependencies>\n\n"
        "## Architecture Notes\n<key modules / API shape, best guess>\n\n"
        "## Open Questions\n<anything a human should confirm>\n"
    )


def _heuristic_description(
    project_name: str,
    ticket_title: str,
    ticket_description: str,
    acceptance_criteria: str,
) -> Optional[str]:
    """Fill the description template from keyword-detected stack, or ``None``.

    Used when no LLM is available. Returns ``None`` when the ticket text
    gives no detectable language, so the caller falls back to asking the
    human rather than guessing blindly.
    """
    blob = "\n".join(
        [ticket_title or "", ticket_description or "", acceptance_criteria or ""]
    )
    stack = parse_stack_from_text(blob)
    if stack is None:
        return None
    lang_display = {
        "nodejs": "Node.js",
        "python": "Python",
        "go": "Go",
        "rust": "Rust",
        "ruby": "Ruby",
        "java": "Java",
        "php": "PHP",
    }.get(stack.language, stack.language)
    overview = (ticket_title or project_name).strip()
    return (
        f"# {project_name}\n\n"
        "## Overview\n"
        f"{overview}. (Inferred by Marcus from an early ticket — please "
        "review and correct.)\n\n"
        "## Tech Stack\n"
        f"- **Language**: {lang_display}\n"
        f"- **Framework**: {stack.framework or 'none'}\n"
        "- **Database**: none\n"
        f"- **Dev server command**: {stack.dev_cmd}\n"
        f"- **Install command**: {stack.install_cmd or 'none'}\n\n"
        "## Architecture Notes\n<!-- Inferred from a single ticket; refine as needed. -->\n\n"
        "## Open Questions\n<!-- Confirm the tech stack above is correct. -->\n"
    )


class ProjectDescriptionInferrer:
    """Infers a project description from ticket content.

    Marcus uses this to auto-populate a project's description the first time
    an agent needs the tech stack, instead of blocking on the human. The
    result is written with :data:`SOURCE_INFERRED`, so a human's later edit
    (which locks the description) always wins.

    Parameters
    ----------
    llm_generate : Optional[Callable[[str], Awaitable[str]]]
        Async callable returning freeform text for a prompt (e.g.
        ``AIAnalysisEngine.generate_text``). When ``None`` (or a call
        fails), a keyword-based heuristic is used, which returns ``None``
        if it cannot even detect a language.
    """

    def __init__(
        self,
        llm_generate: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> None:
        """Initialise the inferrer."""
        self._llm = llm_generate

    async def infer(
        self,
        project_name: str,
        ticket_title: str,
        ticket_description: str,
        acceptance_criteria: str = "",
    ) -> Optional[str]:
        """Return an inferred description markdown, or ``None`` if impossible.

        Tries the LLM first; on any failure or empty result, falls back to
        the keyword heuristic. Returns ``None`` only when neither can
        produce something with a usable tech stack.
        """
        if self._llm is not None:
            prompt = _build_inference_prompt(
                project_name, ticket_title, ticket_description, acceptance_criteria
            )
            try:
                out = await self._llm(prompt)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Project description inference LLM failed: %s", exc)
                out = ""
            if out and out.strip() and parse_stack_from_text(out) is not None:
                return out.strip()
            logger.info(
                "LLM description inference unusable (no parseable stack); "
                "falling back to heuristic"
            )
        return _heuristic_description(
            project_name, ticket_title, ticket_description, acceptance_criteria
        )
