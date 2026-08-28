"""
Writes a "Dev Environment Preview" section into a project's own Gitea
README.md, describing how Marcus (or a human) runs the project locally.

Why this exists: without it, the only place a resolved dev-environment
stack (language, framework, install/run commands — see
``src.core.project_description.ProjectStack``) is visible is Marcus's own
UI. A human cloning the repo directly, or reading it on Gitea, has no way
to know how Marcus actually runs a live preview of it. This mirrors that
resolved stack into the repo itself, inside a clearly marked section so
it never collides with whatever else a human writes in the README.

Isolation from the live preview's own working tree
----------------------------------------------------
This deliberately does NOT touch the same on-disk clone
(``local_repo_path``) that dev-environment previews bind-mount and that
other Marcus flows (branch sync, ticket merges) actively read/write —
switching that shared working tree to ``main`` and hard-resetting it here
could race a concurrently-starting preview's own ``git clone /src /app``
(see ``src/core/dev_environment.py``) reading a half-checked-out state.
Instead this makes its own throwaway, depth-1 clone of the SAME remote
(read from the shared clone's already-configured ``origin``, credentials
and all), edits and commits there, pushes, and discards it.

Functions
---------
update_dev_preview_readme_section
    Render the section for a given stack and push it if it changed.
"""

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional

# Reusing gitea_manager's own git-subprocess helper (timeout-guarded,
# redacts embedded credentials in error messages) rather than
# reimplementing it — see its docstring for exact behavior.
from src.integrations.gitea_manager import (
    GIT_TRANSFER_TIMEOUT_SECONDS,
    _run_git,
)

logger = logging.getLogger(__name__)

_SECTION_START = "<!-- marcus:dev-preview:start -->"
_SECTION_END = "<!-- marcus:dev-preview:end -->"
_SECTION_RE = re.compile(
    re.escape(_SECTION_START) + r".*?" + re.escape(_SECTION_END), re.DOTALL
)

#: Read-only git commands (checking the remote URL) get a short timeout —
#: no push/clone-sized transfer involved.
_GIT_READ_TIMEOUT = 15.0


def _render_section(stack: object) -> str:
    """Render the marked README section for *stack*.

    Parameters
    ----------
    stack : ProjectStack
        The resolved dev-environment stack to describe.

    Returns
    -------
    str
        The section text, including its start/end markers.
    """
    language = str(getattr(stack, "language", "") or "unknown")
    framework = str(getattr(stack, "framework", "") or "")
    install_cmd = str(getattr(stack, "install_cmd", "") or "")
    dev_cmd = str(getattr(stack, "dev_cmd", "") or "")

    lines = [
        _SECTION_START,
        "## Dev Environment Preview",
        "",
        "Marcus runs a live preview of this project using the following "
        "configuration, determined from this repository's own files.",
        "",
        f"- **Language**: {language}",
        f"- **Framework**: {framework or 'none'}",
        f"- **Install**: `{install_cmd or '(none)'}`",
        f"- **Run**: `{dev_cmd}`",
        "",
        "To run it yourself locally:",
        "",
        "```bash",
    ]
    if install_cmd:
        lines.append(install_cmd)
    lines.append(dev_cmd)
    lines += [
        "```",
        "",
        "The dev server listens on port 3000 inside Marcus's preview "
        "container; Marcus publishes it to an automatically-picked host "
        "port and shows you the URL when you start a preview from the "
        "Kanboard board.",
        "",
        "_This section is maintained automatically by Marcus and "
        "overwritten whenever the detected run configuration changes — "
        "edit the rest of this README freely._",
        _SECTION_END,
    ]
    return "\n".join(lines)


def _apply_section(readme_text: Optional[str], section: str, project_name: str) -> str:
    """Insert/replace *section* in *readme_text*, or create a minimal
    README containing it when *readme_text* is ``None`` (no README yet).

    Parameters
    ----------
    readme_text : Optional[str]
        Current README.md content, or ``None`` if the file doesn't exist.
    section : str
        The marked section to insert (see :func:`_render_section`).
    project_name : str
        Used only when creating a brand-new README's title.

    Returns
    -------
    str
        The full new README content.
    """
    if readme_text is None:
        return f"# {project_name}\n\nManaged by Marcus.\n\n{section}\n"
    if _SECTION_RE.search(readme_text):
        return _SECTION_RE.sub(section, readme_text, count=1)
    separator = "\n\n" if not readme_text.endswith("\n\n") else ""
    return readme_text + separator + section + "\n"


async def _git_output(args: List[str], cwd: str, timeout: float = _GIT_READ_TIMEOUT) -> str:
    """Run a read-only git command and return its stdout, or ``""`` on
    any failure.

    Deliberately never logs the command's output — this is used to read
    a remote URL, which may have credentials embedded in it.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return ""
        return stdout_bytes.decode().strip()
    except Exception:  # noqa: BLE001
        return ""


async def update_dev_preview_readme_section(
    repo_path: str, stack: object, main_branch: str = "main"
) -> bool:
    """Write/update the "Dev Environment Preview" section in the repo's
    own README.md on *main_branch*, via a throwaway clone.

    Best-effort and defensive throughout — this must never break the
    dev-environment preview it's describing. Any failure (no remote
    configured, network error, nothing to commit) simply returns
    ``False``; nothing here ever raises.

    Parameters
    ----------
    repo_path : str
        Marcus's own existing local clone of the project (used only to
        read the already-configured, credentialed ``origin`` remote URL
        — never written to).
    stack : ProjectStack
        The resolved dev-environment stack to describe.
    main_branch : str
        Branch to commit the README update to. Defaults to ``"main"``.

    Returns
    -------
    bool
        ``True`` if a commit was actually pushed, ``False`` otherwise
        (nothing changed, or the update failed).
    """
    if not repo_path or not os.path.isdir(repo_path):
        return False
    try:
        origin_url = await _git_output(
            ["git", "remote", "get-url", "origin"], cwd=repo_path
        )
        if not origin_url:
            logger.debug(
                "No origin remote configured for %s — skipping README update",
                repo_path,
            )
            return False

        with tempfile.TemporaryDirectory(prefix="marcus-readme-") as tmp_dir:
            clone_dir = os.path.join(tmp_dir, "repo")
            await _run_git(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    main_branch,
                    origin_url,
                    clone_dir,
                ],
                cwd=tmp_dir,
                timeout=GIT_TRANSFER_TIMEOUT_SECONDS,
            )
            await _run_git(["git", "config", "user.email", "marcus@localhost"], cwd=clone_dir)
            await _run_git(["git", "config", "user.name", "Marcus"], cwd=clone_dir)

            readme_path = Path(clone_dir) / "README.md"
            existing = readme_path.read_text(errors="replace") if readme_path.exists() else None
            project_name = Path(repo_path).name.replace("-", " ").title()
            section = _render_section(stack)
            new_text = _apply_section(existing, section, project_name)

            if existing == new_text:
                return False

            readme_path.write_text(new_text)
            await _run_git(["git", "add", "README.md"], cwd=clone_dir)

            try:
                await _run_git(["git", "diff", "--cached", "--quiet"], cwd=clone_dir)
                return False  # nothing actually staged despite the text diff
            except RuntimeError:
                pass  # non-zero exit means there IS a staged diff — proceed

            await _run_git(
                [
                    "git",
                    "commit",
                    "-m",
                    "docs: update dev-environment preview instructions [marcus]",
                ],
                cwd=clone_dir,
            )
            await _run_git(
                ["git", "push", "origin", f"HEAD:{main_branch}"],
                cwd=clone_dir,
                timeout=GIT_TRANSFER_TIMEOUT_SECONDS,
            )
        logger.info("Updated dev-environment preview instructions in %s's README.md", repo_path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not update dev-preview README section for %s: %s", repo_path, exc
        )
        return False
