"""
AI-based fallback for determining how to run a project's dev-environment
preview, used only when deterministic file-sniffing
(``src.core.dev_environment.detect_project_type``) finds nothing it
recognizes at all (returns ``"static"``) — no ``package.json``,
``requirements.txt``/``pyproject.toml``, ``Cargo.toml``, ``go.mod``,
``Gemfile``, ``pom.xml``/``build.gradle``, or ``composer.json``.

That heuristic-only path means a preview for an unrecognized layout
(a monorepo subdirectory, an unconventional entrypoint name, a language
Marcus's heuristics don't enumerate at all) silently falls back to
serving the repo as static files, even when it plainly is a runnable
application — nothing ever *reads the code* to figure out the real
answer. This module does exactly that: hand Marcus's own AI provider a
compact snapshot of the repo's actual files (README, whichever manifest
files exist, and a few common entrypoint files) and ask it to infer the
same fields :func:`~src.core.project_description.parse_stack_from_text`
would have extracted from a human-written description, but grounded in
the real code instead of prose.

Functions
---------
infer_stack_with_ai
    Gather a repo snapshot, ask the AI provider, and parse its answer
    into a :class:`~src.core.project_description.ProjectStack`.
"""

import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from src.core.project_description import ProjectStack
from src.utils.json_parser import parse_ai_json_response

logger = logging.getLogger(__name__)

#: Directories never worth listing or descending into — build artifacts,
#: dependency caches, and VCS internals carry no information about how a
#: human would actually run the project.
_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".tox",
    "vendor",
    "target",
}

#: Manifest/config files worth including in full (truncated) when present
#: — the strongest signal of intended language/framework and how to
#: install + run it.
_MANIFEST_FILES = (
    "README.md",
    "README.rst",
    "README",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "Dockerfile",
    "docker-compose.yml",
    "Procfile",
)

#: Common entrypoint filenames — checked after the manifests above, since
#: their mere presence (even with only a short excerpt) can disambiguate
#: framework/runtime when a manifest alone can't (e.g. a bare
#: ``requirements.txt`` next to ``manage.py`` vs. ``app.py``).
_ENTRYPOINT_FILES = (
    "manage.py",
    "app.py",
    "main.py",
    "wsgi.py",
    "asgi.py",
    "run.py",
    "server.js",
    "index.js",
    "app.js",
)

#: Per-file and total snapshot size caps, in characters — keeps the
#: prompt small and cheap regardless of how large the actual files are.
_MAX_FILE_CHARS = 3000
_MAX_TOTAL_CHARS = 14000
_MAX_LISTED_ENTRIES = 100


def _list_top_level(repo_path: str) -> List[str]:
    """Return a capped, sorted top-level directory listing of *repo_path*,
    skipping VCS/dependency/build directories that carry no signal.
    """
    try:
        entries = sorted(os.listdir(repo_path))
    except OSError as exc:
        logger.debug("Could not list repo %s: %s", repo_path, exc)
        return []
    shown = [e for e in entries if e not in _SKIP_DIRS and not e.startswith(".git")]
    return shown[:_MAX_LISTED_ENTRIES]


def _read_truncated(path: Path, limit: int = _MAX_FILE_CHARS) -> Optional[str]:
    """Read *path* as text, truncated to *limit* characters. Returns
    ``None`` if the file doesn't exist or can't be read as text.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    if len(text) > limit:
        text = text[:limit] + "\n... (truncated)"
    return text


def _build_repo_snapshot(repo_path: str) -> str:
    """Assemble a compact, bounded text snapshot of *repo_path* for the
    LLM prompt: a top-level file listing, then the content of whichever
    manifest/entrypoint files actually exist.

    Parameters
    ----------
    repo_path : str
        Root of the cloned git repository to inspect.

    Returns
    -------
    str
        The snapshot text, capped at :data:`_MAX_TOTAL_CHARS`.
    """
    root = Path(repo_path)
    sections: List[str] = []

    listing = _list_top_level(repo_path)
    if listing:
        sections.append("Top-level files/directories:\n" + "\n".join(listing))

    for name in _MANIFEST_FILES + _ENTRYPOINT_FILES:
        content = _read_truncated(root / name)
        if content is not None:
            sections.append(f"--- {name} ---\n{content}")

    snapshot = "\n\n".join(sections)
    if len(snapshot) > _MAX_TOTAL_CHARS:
        snapshot = snapshot[:_MAX_TOTAL_CHARS] + "\n... (snapshot truncated)"
    return snapshot


def _single_line(text: str) -> str:
    """Collapse any embedded newlines/carriage returns in *text* to
    spaces, and squeeze repeated whitespace.

    The persisted project description stores ``dev_cmd``/``install_cmd``/
    ``framework`` as single markdown list-item lines (see
    :func:`~src.marcus_mcp.server._persist_detected_stack`), and
    :func:`~src.core.project_description._extract_field` only ever reads
    the first line of a field's value back out. Every OTHER stack source
    (:data:`~src.core.dev_environment.STACK_CONFIGS`, human-written text)
    is single-line by construction, so this was never reachable before —
    but an LLM's free-text JSON response can legitimately contain a
    literal embedded newline (e.g. a multi-step command). Without this,
    such a value round-trips fine the first time, then silently
    truncates to its first line (e.g. a dangling ``"cd backend &&"``) on
    every later preview start once it's re-parsed from the stored
    description — confirmed empirically against the real parser.

    Parameters
    ----------
    text : str
        Raw text that must be safe to store as one markdown line.

    Returns
    -------
    str
        *text* with all whitespace runs (including newlines) collapsed
        to single spaces, stripped.
    """
    return " ".join(text.split())


def _build_prompt(snapshot: str) -> str:
    """Build the LLM prompt asking for a strict-JSON run configuration.

    The instructions constrain the answer to what the dev-environment
    container can actually execute (see
    ``src/core/dev_environment.py``'s module docstring): an Alpine
    3.20-based container with no language runtime preinstalled, the app
    must bind ``0.0.0.0:3000``, and package names must be Alpine ``apk``
    names, not Debian/Ubuntu ones.
    """
    return (
        "You are looking at a snapshot of a software repository's files "
        "below. Determine the best way to install its dependencies and "
        "start it as a local dev server for a live preview.\n\n"
        "Constraints on your answer:\n"
        "- The preview runs inside an Alpine Linux 3.20 container with NO "
        "language runtime preinstalled — list every Alpine `apk` package "
        "name needed (e.g. python3/py3-pip, nodejs/npm, go, rust/cargo, "
        "ruby, openjdk17/maven, php).\n"
        "- The dev server MUST bind host 0.0.0.0 and port 3000.\n"
        "- If you cannot confidently determine how to run this project "
        'from the files shown, respond with {"language": ""} exactly — '
        "do not guess.\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no prose) "
        "with these exact fields:\n"
        '{\n'
        '  "language": "python|nodejs|go|rust|ruby|java|php|static",\n'
        '  "framework": "short name, or empty string if none/unknown",\n'
        '  "install_cmd": "shell command to install dependencies, or empty",\n'
        '  "dev_cmd": "shell command to start the dev server on 0.0.0.0:3000",\n'
        '  "use_hot_reload": true or false,\n'
        '  "apk_packages": ["list", "of", "alpine", "package", "names"]\n'
        "}\n\n"
        "Repository snapshot:\n\n"
        f"{snapshot}\n"
    )


async def infer_stack_with_ai(
    repo_path: str, generate_text: Callable[[str], Awaitable[str]]
) -> Optional[ProjectStack]:
    """Read *repo_path*'s own files with an LLM and infer how to run it.

    Best-effort and defensive throughout: any failure (no files to read,
    the AI call raising, an unparseable or incomplete response) yields
    ``None`` rather than raising, so a flaky or slow AI provider can
    never break dev-environment preview start — the caller falls back to
    whatever it already had.

    Parameters
    ----------
    repo_path : str
        Root of the cloned git repository to inspect.
    generate_text : Callable[[str], Awaitable[str]]
        Marcus's own AI provider call, e.g. ``server.ai_engine.
        generate_text`` — provider-agnostic (works the same whether
        Marcus is configured for ``claude_subscription`` or an API key).

    Returns
    -------
    Optional[ProjectStack]
        The inferred stack, or ``None`` if nothing usable could be
        determined.
    """
    try:
        snapshot = _build_repo_snapshot(repo_path)
        if not snapshot.strip():
            logger.debug("Repo %s has nothing to analyze — skipping AI inference", repo_path)
            return None

        prompt = _build_prompt(snapshot)
        response = await generate_text(prompt)
        data = parse_ai_json_response(response)

        language = str(data.get("language") or "").strip().lower()
        if not language:
            logger.info(
                "AI stack inference for %s: could not determine a run "
                "configuration",
                repo_path,
            )
            return None

        if language == "static":
            # A confident "static" answer is a legitimate, USABLE result
            # (see the prompt's own schema, which lists it as a valid
            # language value) — not the same as "couldn't determine
            # anything". Building this from STACK_CONFIGS keeps it
            # identical to the deterministic detect_project_type("static")
            # fallback rather than inventing a second copy of those values.
            from src.core.dev_environment import STACK_CONFIGS

            fb = STACK_CONFIGS["static"]
            logger.info(
                "AI stack inference for %s: repo is static content", repo_path
            )
            return ProjectStack(
                language="static",
                framework="",
                install_cmd=fb["install"],
                dev_cmd=fb["start"],
                use_hm_reload=fb["hm"],
                extra_apt=list(fb.get("apk", [])),
            )

        dev_cmd = _single_line(str(data.get("dev_cmd") or ""))
        if not dev_cmd:
            logger.info(
                "AI stack inference for %s returned no usable run "
                "configuration (language=%r)",
                repo_path,
                language,
            )
            return None

        apk_packages = data.get("apk_packages")
        extra_apt = (
            [str(p) for p in apk_packages if str(p).strip()]
            if isinstance(apk_packages, list)
            else []
        )

        stack = ProjectStack(
            language=language,
            framework=_single_line(str(data.get("framework") or "")),
            install_cmd=_single_line(str(data.get("install_cmd") or "")),
            dev_cmd=dev_cmd,
            use_hm_reload=bool(data.get("use_hot_reload", False)),
            extra_apt=extra_apt,
        )
        logger.info(
            "AI stack inference for %s: language=%r framework=%r",
            repo_path,
            stack.language,
            stack.framework,
        )
        return stack
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI stack inference failed for %s: %s", repo_path, exc)
        return None
