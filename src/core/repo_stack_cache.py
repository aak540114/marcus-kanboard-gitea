"""
Per-project cache for repo-derived dev-environment stack decisions.

Two things are cached here, both keyed by Kanboard project id:

1. **AI-inferred stacks** (``ai_stacks``) — the result of asking Marcus's
   own AI provider to read a repo's files and infer how to run it (see
   :mod:`src.core.repo_stack_inference`). This step only runs when
   deterministic file-sniffing (``detect_project_type``) found nothing
   recognizable, and an LLM call has real latency/cost — so the result is
   cached against the repo's current commit SHA and only recomputed when
   that SHA changes.
2. **The last stack written into the repo's own README.md**
   (``readme_hashes``) — a hash of the resolved stack's key fields, so
   the dev-environment preview start path can skip the git fetch/commit/
   push round trip entirely when nothing has actually changed since the
   last write, and only pay that cost when the resolved stack (however it
   was resolved — declared, corrected, or AI-inferred) actually differs.

Persisted as a JSON file at::

    <data_dir>/repo_stack_cache.json

Schema::

    {
      "ai_stacks": {
        "7": {
          "fingerprint": "<git HEAD sha>",
          "stack": {
            "language": "python", "framework": "Django",
            "install_cmd": "...", "dev_cmd": "...",
            "use_hm_reload": false, "extra_apt": ["python3", "py3-pip"]
          },
          "computed_at": "2026-08-28T12:00:00+00:00"
        }
      },
      "readme_hashes": {"7": "a1b2c3..."}
    }

Classes
-------
RepoStackCache
    Reads and writes both caches above.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path(os.getcwd()) / "data"


def stack_hash(stack: Any) -> str:
    """Fingerprint the fields of a ``ProjectStack`` that matter for the
    README's "Dev Environment Preview" section.

    Only the fields actually rendered into that section are hashed
    (language, framework, install_cmd, dev_cmd) — ``use_hm_reload`` and
    ``extra_apt`` are internal container-build details a human reading
    the README doesn't need surfaced, so a change to only those would not
    be worth a commit.

    Parameters
    ----------
    stack : ProjectStack
        The resolved stack for a dev-environment preview.

    Returns
    -------
    str
        A short, stable hex digest.
    """
    parts = "|".join(
        [
            str(getattr(stack, "language", "") or ""),
            str(getattr(stack, "framework", "") or ""),
            str(getattr(stack, "install_cmd", "") or ""),
            str(getattr(stack, "dev_cmd", "") or ""),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


class RepoStackCache:
    """Caches AI-inferred stacks and README-write state, per project.

    Parameters
    ----------
    data_dir : Optional[Path]
        Directory that contains ``repo_stack_cache.json``. Defaults to
        ``./data/`` relative to the Marcus working directory.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._path = (data_dir or _DEFAULT_DATA_DIR) / "repo_stack_cache.json"
        self._data: Dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # AI-inferred stack cache
    # ------------------------------------------------------------------

    def get_ai_stack(self, project_id: int) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Return ``(fingerprint, stack_fields)`` cached for *project_id*,
        or ``None`` if nothing is cached.

        The caller compares the returned fingerprint against the repo's
        CURRENT commit SHA — a match means the cached stack is still
        fresh; a mismatch means the repo changed since the AI last read
        it and the analysis should be redone.
        """
        entry = (self._data.get("ai_stacks") or {}).get(str(project_id))
        if not entry:
            return None
        fingerprint = entry.get("fingerprint")
        stack_fields = entry.get("stack")
        if not fingerprint or not isinstance(stack_fields, dict):
            return None
        return fingerprint, stack_fields

    def store_ai_stack(
        self, project_id: int, fingerprint: str, stack_fields: Dict[str, Any]
    ) -> None:
        """Cache an AI-inferred stack for *project_id* against *fingerprint*
        (the repo's commit SHA at analysis time) and persist to disk.
        """
        from datetime import datetime, timezone

        self._data.setdefault("ai_stacks", {})[str(project_id)] = {
            "fingerprint": fingerprint,
            "stack": stack_fields,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    # ------------------------------------------------------------------
    # README-write state
    # ------------------------------------------------------------------

    def get_readme_hash(self, project_id: int) -> Optional[str]:
        """Return the stack hash last written into *project_id*'s README,
        or ``None`` if the dev-preview section has never been written.
        """
        val = (self._data.get("readme_hashes") or {}).get(str(project_id))
        return str(val) if val else None

    def store_readme_hash(self, project_id: int, hash_value: str) -> None:
        """Record that *hash_value* is now what's written into the repo's
        README, and persist to disk.
        """
        self._data.setdefault("readme_hashes", {})[str(project_id)] = hash_value
        self._save()

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        """Load the cache from disk; return an empty structure on any
        error. Fails safe: a missing or corrupt file just means every
        project's next preview start recomputes/rewrites once — far less
        harmful than crashing preview start.
        """
        if not self._path.exists():
            return {"ai_stacks": {}, "readme_hashes": {}}
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {"ai_stacks": {}, "readme_hashes": {}}
            data.setdefault("ai_stacks", {})
            data.setdefault("readme_hashes", {})
            return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read repo_stack_cache.json: %s", exc)
            return {"ai_stacks": {}, "readme_hashes": {}}

    def _save(self) -> None:
        """Persist the cache to disk via a temp-file + ``os.replace`` swap
        (matches ``ProjectStatsManager._save``'s pattern) so a process
        killed mid-write never leaves a truncated file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp_path, self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save repo_stack_cache.json: %s", exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
