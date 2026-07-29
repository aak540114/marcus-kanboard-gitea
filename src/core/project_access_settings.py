"""
Per-project access control: is Marcus allowed to work on this Kanboard
project's tickets at all?

This is a SEPARATE axis from gate mode (``src/core/gate_settings.py``): gate
mode governs HOW Marcus works on a project it is already allowed to touch
(pause for human review vs. work autonomously); this module governs WHETHER
it may touch that project's tickets in the first place. With multiple
Kanboard projects in play, a human may want Marcus scoped to only some of
them — a brand-new project should not silently start getting Gitea repos,
claimed tickets, and AI agent commits the moment it is created.

Default is OFF: a project with no explicit setting is NOT enabled. A human
opts a project in via the Kanboard board header's "Marcus" toggle (or the
``/api/project-enabled`` route directly).

Settings are persisted as a JSON file at::

    <data_dir>/project_access_settings.json

Schema::

    {"projects": {"7": {"enabled": true}, "2": {"enabled": false}}}

An absent project key means "never configured" (resolves to disabled via
:meth:`is_enabled`, same as an explicit ``false`` — the two are
distinguished only by :meth:`get_project_enabled`, for UI/diagnostics).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path(os.getcwd()) / "data"


class ProjectAccessSettingManager:
    """Reads and writes per-project "is Marcus allowed here" settings.

    Parameters
    ----------
    data_dir : Optional[Path]
        Directory that contains ``project_access_settings.json``. Defaults
        to ``./data/`` relative to the Marcus working directory.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._path = (data_dir or _DEFAULT_DATA_DIR) / "project_access_settings.json"
        self._data: Dict[str, Any] = self._load()

    def is_enabled(self, project_id: int) -> bool:
        """Return whether Marcus is allowed to work on *project_id*.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        bool
            ``True`` only if a human has explicitly enabled this project.
            Defaults to ``False`` — a never-configured project is disabled,
            not permitted.
        """
        return self.get_project_enabled(project_id) is True

    def get_project_enabled(self, project_id: int) -> Optional[bool]:
        """Return the EXPLICIT stored setting for a project, or ``None``.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[bool]
            ``True``/``False`` if a human has explicitly set this project,
            or ``None`` if it has never been configured. Both ``False`` and
            ``None`` mean "not permitted" via :meth:`is_enabled` — this
            getter exists to distinguish "never touched" from "explicitly
            revoked" for the UI.
        """
        val = self._project_entry(project_id).get("enabled")
        return val if isinstance(val, bool) else None

    def enabled_project_ids(self) -> List[int]:
        """Return every project a human has explicitly enabled for Marcus.

        This is the set of boards Marcus may read and work — it is NOT the
        same as the single ``kanboard_project_id`` from config, which only
        records the project setup.sh happened to provision. Marcus polls
        these, so a project enabled from its board header is picked up
        without touching any configuration.

        Returns
        -------
        List[int]
            Enabled project ids, ascending. Empty when nothing has been
            enabled — the default-off state, in which Marcus reads no board.
        """
        out: List[int] = []
        for key, entry in (self._data.get("projects") or {}).items():
            if not isinstance(entry, dict) or entry.get("enabled") is not True:
                continue
            try:
                out.append(int(key))
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric project key %r", key)
        return sorted(out)

    def set_project_enabled(self, project_id: int, enabled: bool) -> None:
        """Persist whether Marcus is allowed to work on *project_id*.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        enabled : bool
            ``True`` to allow Marcus (and AI agents) to claim and work this
            project's tickets; ``False`` to block new claims (tickets
            already in progress are not forcibly interrupted).
        """
        self._project_entry(project_id, create=True)["enabled"] = enabled
        self._save()
        logger.info(
            "Project %d Marcus access set to %s", project_id, "enabled" if enabled else "disabled"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _project_entry(self, project_id: int, *, create: bool = False) -> Dict[str, Any]:
        """Return (and optionally create) the dict for a project."""
        key = str(project_id)
        projects = self._data.setdefault("projects", {})
        if key not in projects:
            if create:
                projects[key] = {}
            else:
                return {}
        return dict(projects[key]) if not create else projects[key]

    def _load(self) -> Dict[str, Any]:
        """Load settings from disk; return an empty structure on any error.

        Fails SAFE: a missing or corrupt file yields no enabled projects
        (every project stays disabled) rather than raising, since this
        file gates whether Marcus/AI agents may act at all.
        """
        if not self._path.exists():
            return {"projects": {}}
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {"projects": {}}
            data.setdefault("projects", {})
            return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read project_access_settings.json: %s", exc)
            return {"projects": {}}

    def _save(self) -> None:
        """Write settings to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
        except OSError as exc:
            logger.error("Could not write project_access_settings.json: %s", exc)
