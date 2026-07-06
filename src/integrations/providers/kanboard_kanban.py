"""
Kanboard kanban provider for Marcus.

Connects Marcus to a self-hosted Kanboard instance via its built-in
JSON-RPC 2.0 API (``/jsonrpc.php``).  No Kanboard source modifications
are required — the API ships with every Kanboard installation.

Current state: fully functional for the core workflow (connect, read
tasks, create/update/assign tasks, move columns, add comments, report
blockers, project metrics).  File attachment upload and download are
implemented as best-effort wrappers around Kanboard's file API.

See https://docs.kanboard.org/v1/api/ for the full API reference.

Classes
-------
KanboardKanban
    Kanboard JSON-RPC 2.0 implementation of KanbanInterface.
"""

import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import httpx

from src.core.models import Priority, Task, TaskStatus
from src.integrations.kanban_interface import KanbanInterface, KanbanProvider

logger = logging.getLogger(__name__)

# Kanboard column name (lower-cased) → Marcus TaskStatus.
# Columns are user-defined, so this covers common naming conventions.
_COLUMN_STATUS_MAP: Dict[str, TaskStatus] = {
    # TODO-family
    "backlog": TaskStatus.TODO,
    "todo": TaskStatus.TODO,
    "to do": TaskStatus.TODO,
    "open": TaskStatus.TODO,
    "new": TaskStatus.TODO,
    "queue": TaskStatus.TODO,
    # READY-family — human-gated workflow trigger column
    "ready": TaskStatus.READY,
    # IN_PROGRESS-family
    "in progress": TaskStatus.IN_PROGRESS,
    "in development": TaskStatus.IN_PROGRESS,
    "wip": TaskStatus.IN_PROGRESS,
    "work in progress": TaskStatus.IN_PROGRESS,
    "doing": TaskStatus.IN_PROGRESS,
    "active": TaskStatus.IN_PROGRESS,
    "development": TaskStatus.IN_PROGRESS,
    "review": TaskStatus.IN_PROGRESS,
    "in review": TaskStatus.IN_PROGRESS,
    "testing": TaskStatus.IN_PROGRESS,
    # WAITING_FOR_HUMAN-family
    "waiting for human": TaskStatus.WAITING_FOR_HUMAN,
    "waiting": TaskStatus.WAITING_FOR_HUMAN,
    "pending review": TaskStatus.WAITING_FOR_HUMAN,
    # BLOCKED-family
    "blocked": TaskStatus.BLOCKED,
    "block": TaskStatus.BLOCKED,
    "impediment": TaskStatus.BLOCKED,
    "on hold": TaskStatus.BLOCKED,
    "hold": TaskStatus.BLOCKED,
    # DONE-family
    "done": TaskStatus.DONE,
    "closed": TaskStatus.DONE,
    "complete": TaskStatus.DONE,
    "completed": TaskStatus.DONE,
    "finished": TaskStatus.DONE,
    "resolved": TaskStatus.DONE,
    "archive": TaskStatus.DONE,
    "archived": TaskStatus.DONE,
}

# Kanboard priority integer (0–3) → Marcus Priority.
_PRIORITY_MAP: Dict[int, Priority] = {
    0: Priority.LOW,
    1: Priority.MEDIUM,
    2: Priority.HIGH,
    3: Priority.URGENT,
}


class KanboardKanban(KanbanInterface):
    """
    Kanboard JSON-RPC 2.0 implementation of KanbanInterface.

    Authenticates using the global Kanboard API token (Basic Auth with
    username ``jsonrpc``).  Discovered at Kanboard Settings → API.

    Parameters
    ----------
    config : Dict[str, Any]
        Required keys:

        ``kanboard_url``
            Full URL to the Kanboard JSON-RPC endpoint, e.g.
            ``http://localhost:8080/jsonrpc.php``.  If you omit the
            path, ``/jsonrpc.php`` is appended automatically.
        ``kanboard_api_token``
            Global API token shown under Kanboard Settings → API.

        Optional keys:

        ``kanboard_project_id``
            Numeric project ID to scope all queries (default: ``1``).
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialize KanboardKanban with connection config."""
        super().__init__(config)
        self.provider = KanbanProvider.KANBOARD

        url = config["kanboard_url"].rstrip("/")
        if not url.endswith("/jsonrpc.php"):
            url = url + "/jsonrpc.php"
        self._jsonrpc_url: str = url

        self._api_token: str = config["kanboard_api_token"]
        self._project_id: int = int(config.get("kanboard_project_id", 1))

        # column name (lower) → column id — populated in connect()
        self._column_map: Dict[str, int] = {}
        # column id → TaskStatus — populated in connect()
        self._column_status_map: Dict[int, TaskStatus] = {}
        # project name — populated in connect()
        self._project_name: str = ""

        self._client: Optional[httpx.AsyncClient] = None
        self._rpc_id: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """
        Open an authenticated HTTP session and verify credentials.

        Calls ``getProjectById`` as a lightweight credential + project
        check.  Caches the column list for fast ``move_task_to_column``
        lookups.

        Returns
        -------
        bool
            ``True`` if the connection and credential check succeeded.
        """
        self._client = httpx.AsyncClient(
            auth=("jsonrpc", self._api_token),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )
        try:
            project = await self._rpc("getProjectById", project_id=self._project_id)
            if not project:
                logger.error(
                    "Kanboard project %d not found — check kanboard_project_id",
                    self._project_id,
                )
                await self._client.aclose()
                self._client = None
                return False

            self._project_name = project.get("name", "")
            await self._refresh_columns()
            logger.info(
                "Connected to Kanboard project '%s' (id=%d) at %s",
                self._project_name,
                self._project_id,
                self._jsonrpc_url,
            )
            return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Kanboard auth failed (%s): %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            return False
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.error("Kanboard connection error: %s", exc)
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            return False

    async def disconnect(self) -> None:
        """Close the HTTP session."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Task retrieval
    # ------------------------------------------------------------------

    async def get_all_tasks(self) -> List[Task]:
        """
        Fetch all active and closed tasks for the configured project.

        Returns
        -------
        List[Task]
            All tasks converted to Marcus ``Task`` objects.

        Raises
        ------
        RuntimeError
            If ``connect()`` has not been called first.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before get_all_tasks()")

        active = await self._rpc(
            "getAllTasks", project_id=self._project_id, status_id=1
        )
        closed = await self._rpc(
            "getAllTasks", project_id=self._project_id, status_id=0
        )
        tasks: List[Task] = []
        for raw in (active or []) + (closed or []):
            tasks.append(self._to_task(raw))
        return tasks

    async def get_available_tasks(self) -> List[Task]:
        """
        Return unassigned tasks in a TODO or READY column.

        Returns
        -------
        List[Task]
            Unassigned tasks in the TODO or READY column that an agent can claim.
        """
        all_tasks = await self.get_all_tasks()
        return [
            t
            for t in all_tasks
            if t.status in (TaskStatus.TODO, TaskStatus.READY) and not t.assigned_to
        ]

    async def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """
        Fetch a single task by its Kanboard task ID.

        Parameters
        ----------
        task_id : str
            Numeric Kanboard task ID as a string.

        Returns
        -------
        Optional[Task]
            The task, or ``None`` if not found.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before get_task_by_id()")
        raw = await self._rpc("getTask", task_id=int(task_id))
        return self._to_task(raw) if raw else None

    # ------------------------------------------------------------------
    # Task mutation
    # ------------------------------------------------------------------

    async def create_task(self, task_data: Dict[str, Any]) -> Task:
        """
        Create a new Kanboard task.

        Parameters
        ----------
        task_data : Dict[str, Any]
            Expected keys: ``name`` (required), ``description``,
            ``priority``, ``labels``, ``estimated_hours``.

        Returns
        -------
        Task
            The newly created task.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before create_task()")

        priority_str = task_data.get("priority", "medium")
        kb_priority = _marcus_priority_to_kb(priority_str)
        estimated_seconds = int(float(task_data.get("estimated_hours", 0)) * 3600)

        task_id = await self._rpc(
            "createTask",
            project_id=self._project_id,
            title=task_data.get("name", ""),
            description=task_data.get("description", ""),
            priority=kb_priority,
            time_estimated=estimated_seconds,
        )
        if not task_id:
            raise RuntimeError("Kanboard createTask returned no task ID")

        raw = await self._rpc("getTask", task_id=int(task_id))
        return self._to_task(raw)

    async def update_task(
        self, task_id: str, updates: Dict[str, Any]
    ) -> Optional[Task]:
        """
        Apply a partial update to an existing task.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.
        updates : Dict[str, Any]
            Fields to update (``name``, ``description``, ``priority``,
            ``estimated_hours``).

        Returns
        -------
        Optional[Task]
            Updated task, or ``None`` on failure.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before update_task()")

        kb_updates: Dict[str, Any] = {"id": int(task_id)}
        if "name" in updates:
            kb_updates["title"] = updates["name"]
        if "description" in updates:
            kb_updates["description"] = updates["description"]
        if "priority" in updates:
            kb_updates["priority"] = _marcus_priority_to_kb(updates["priority"])
        if "estimated_hours" in updates:
            kb_updates["time_estimated"] = int(float(updates["estimated_hours"]) * 3600)

        success = await self._rpc("updateTask", **kb_updates)
        if not success:
            return None
        raw = await self._rpc("getTask", task_id=int(task_id))
        return self._to_task(raw) if raw else None

    async def assign_task(self, task_id: str, assignee_id: str) -> bool:
        """
        Assign a task to a Kanboard user.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.
        assignee_id : str
            Kanboard user ID (numeric string) or username.  When a
            non-numeric string is supplied, Marcus searches Kanboard
            users by username; if no match is found the assignment is
            recorded as a comment instead.

        Returns
        -------
        bool
            ``True`` on success.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before assign_task()")

        owner_id = await self._resolve_user_id(assignee_id)
        if owner_id is not None:
            result = await self._rpc("updateTask", id=int(task_id), owner_id=owner_id)
            return bool(result)

        # Fall back to recording the assignee as a comment
        return await self.add_comment(task_id, f"[Marcus] Assigned to: {assignee_id}")

    async def move_task_to_column(self, task_id: str, column_name: str) -> bool:
        """
        Move a task to a named column.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.
        column_name : str
            Target column name (case-insensitive).

        Returns
        -------
        bool
            ``True`` on success.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before move_task_to_column()")

        column_id = self._column_map.get(column_name.lower())
        if column_id is None:
            # Try partial match
            for name, cid in self._column_map.items():
                if column_name.lower() in name or name in column_name.lower():
                    column_id = cid
                    break

        if column_id is None:
            logger.warning(
                "Kanboard column '%s' not found in project %d. " "Available: %s",
                column_name,
                self._project_id,
                list(self._column_map.keys()),
            )
            return False

        result = await self._rpc(
            "moveTaskPosition",
            project_id=self._project_id,
            task_id=int(task_id),
            column_id=column_id,
            position=1,
            swimlane_id=0,
        )

        # moveTaskPosition returns True/False or a boolean-like value
        if result:
            # Update closed/open flag based on target column status
            target_status = self._column_status_map.get(column_id)
            if target_status == TaskStatus.DONE:
                await self._rpc("closeTask", task_id=int(task_id))
            else:
                await self._rpc("openTask", task_id=int(task_id))
        return bool(result)

    async def add_comment(self, task_id: str, comment: str) -> bool:
        """
        Append a text comment to a task.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.
        comment : str
            Comment text (Markdown supported by Kanboard).

        Returns
        -------
        bool
            ``True`` on success.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before add_comment()")
        try:
            result = await self._rpc(
                "createComment",
                task_id=int(task_id),
                user_id=0,  # 0 = system/API user
                content=comment,
            )
            return bool(result)
        except Exception as exc:
            logger.error("add_comment failed for task %s: %s", task_id, exc)
            return False

    async def get_project_metrics(self) -> Dict[str, Any]:
        """
        Return task counts by status for the configured project.

        Returns
        -------
        Dict[str, Any]
            Keys: ``total_tasks``, ``backlog_tasks``, ``in_progress_tasks``,
            ``completed_tasks``, ``blocked_tasks``.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before get_project_metrics()")

        all_tasks = await self.get_all_tasks()
        metrics: Dict[str, Any] = {
            "total_tasks": len(all_tasks),
            "backlog_tasks": sum(1 for t in all_tasks if t.status == TaskStatus.TODO),
            "in_progress_tasks": sum(
                1 for t in all_tasks if t.status == TaskStatus.IN_PROGRESS
            ),
            "completed_tasks": sum(1 for t in all_tasks if t.status == TaskStatus.DONE),
            "blocked_tasks": sum(
                1 for t in all_tasks if t.status == TaskStatus.BLOCKED
            ),
        }
        return metrics

    async def report_blocker(
        self,
        task_id: str,
        blocker_description: str,
        severity: str = "medium",
    ) -> bool:
        """
        Mark a task as blocked and record the blocker reason.

        Moves the task to the first column whose name matches the
        ``blocked`` family (e.g. "Blocked"), then adds a comment.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.
        blocker_description : str
            Human-readable explanation of what is blocking progress.
        severity : str
            Blocker severity: ``low``, ``medium``, or ``high``.

        Returns
        -------
        bool
            ``True`` on success.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before report_blocker()")

        comment = f"[Marcus BLOCKER — {severity.upper()}]\n\n{blocker_description}"
        await self.add_comment(task_id, comment)

        # Try to move to a "Blocked" column; failure is non-fatal
        await self.move_task_to_column(task_id, "Blocked")
        return True

    async def update_task_progress(
        self, task_id: str, progress_data: Dict[str, Any]
    ) -> bool:
        """
        Record agent progress on a task via a comment.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.
        progress_data : Dict[str, Any]
            Expected keys: ``progress`` (0–100), ``status``, ``message``.

        Returns
        -------
        bool
            ``True`` on success.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before update_task_progress()")

        progress = progress_data.get("progress", 0)
        status = progress_data.get("status", "")
        message = progress_data.get("message", "")

        comment = f"[Marcus] Progress: {progress}%"
        if status:
            comment += f" | Status: {status}"
        if message:
            comment += f"\n\n{message}"

        # Move to In Progress when work starts (but never auto-close;
        # closing is a human action gated by HumanGatedWorkflow).
        if 0 < progress < 100:
            await self.move_task_to_column(task_id, "In Progress")

        return await self.add_comment(task_id, comment)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    async def upload_attachment(
        self,
        task_id: str,
        filename: str,
        content: Union[str, bytes],
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload a file attachment to a Kanboard task.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.
        filename : str
            Destination filename.
        content : Union[str, bytes]
            File content — bytes or a base64-encoded string.
        content_type : Optional[str]
            MIME type (not used by Kanboard; stored for compatibility).

        Returns
        -------
        Dict[str, Any]
            ``{success, data: {id, filename}}`` on success.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before upload_attachment()")
        try:
            if isinstance(content, bytes):
                blob = base64.b64encode(content).decode("ascii")
            else:
                blob = content  # assume already base64

            file_id = await self._rpc(
                "createTaskFile",
                project_id=self._project_id,
                task_id=int(task_id),
                filename=filename,
                blob=blob,
            )
            if file_id:
                return {
                    "success": True,
                    "data": {"id": str(file_id), "filename": filename},
                }
            return {"success": False, "error": "Kanboard createTaskFile returned no ID"}
        except Exception as exc:
            logger.error("upload_attachment failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        """
        List all file attachments for a task.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.

        Returns
        -------
        Dict[str, Any]
            ``{success, data: [{id, filename, created_at}]}``
        """
        if self._client is None:
            raise RuntimeError("Call connect() before get_attachments()")
        try:
            files = await self._rpc("getAllTaskFiles", task_id=int(task_id))
            items = [
                {
                    "id": str(f.get("id", "")),
                    "filename": f.get("name", ""),
                    "created_at": f.get("date", ""),
                    "created_by": str(f.get("user_id", "")),
                    "url": f.get("path", ""),
                }
                for f in (files or [])
            ]
            return {"success": True, "data": items}
        except Exception as exc:
            logger.error("get_attachments failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def download_attachment(
        self,
        attachment_id: str,
        filename: str,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Retrieve a file attachment as base64-encoded content.

        Parameters
        ----------
        attachment_id : str
            Kanboard file ID.
        filename : str
            Expected filename (used as a hint for content-type detection).
        task_id : Optional[str]
            Kanboard task ID (required by Kanboard's download endpoint).

        Returns
        -------
        Dict[str, Any]
            ``{success, data: {content: base64str, filename, content_type}}``
        """
        if self._client is None:
            raise RuntimeError("Call connect() before download_attachment()")
        try:
            # Kanboard's JSON-RPC getTaskFile returns metadata, not content.
            # Content is served via HTTP from /projects/.../files/...
            # We build that URL and fetch it with our authenticated client.
            meta = await self._rpc("getTaskFile", file_id=int(attachment_id))
            if not meta:
                return {"success": False, "error": "File not found"}

            file_path = meta.get("path", "")
            if not file_path:
                return {"success": False, "error": "No download path available"}

            # file_path is relative to the Kanboard base URL
            base = self._jsonrpc_url.replace("/jsonrpc.php", "")
            download_url = f"{base}/{file_path.lstrip('/')}"
            response = await self._client.get(download_url)
            response.raise_for_status()
            encoded = base64.b64encode(response.content).decode("ascii")
            ct = response.headers.get("content-type", "application/octet-stream")
            return {
                "success": True,
                "data": {
                    "content": encoded,
                    "filename": meta.get("name", filename),
                    "content_type": ct,
                },
            }
        except Exception as exc:
            logger.error("download_attachment failed: %s", exc)
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    def normalize_status(self, provider_status: Any) -> TaskStatus:
        """
        Map a Kanboard column name to a Marcus ``TaskStatus``.

        Parameters
        ----------
        provider_status : Any
            Column name string from Kanboard.

        Returns
        -------
        TaskStatus
            Matching status, defaulting to ``TODO`` for unknown names.
        """
        if isinstance(provider_status, str):
            return _COLUMN_STATUS_MAP.get(provider_status.lower(), TaskStatus.TODO)
        return TaskStatus.TODO

    def normalize_priority(self, provider_priority: Any) -> Priority:
        """
        Map a Kanboard priority integer to a Marcus ``Priority``.

        Parameters
        ----------
        provider_priority : Any
            Integer (0–3) from Kanboard's priority field.

        Returns
        -------
        Priority
            Matching priority, defaulting to ``MEDIUM`` for unknown values.
        """
        try:
            return _PRIORITY_MAP.get(int(provider_priority), Priority.MEDIUM)
        except (TypeError, ValueError):
            return Priority.MEDIUM

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _rpc(self, method: str, **params: Any) -> Any:
        """
        Make a JSON-RPC 2.0 call and return the ``result`` field.

        Parameters
        ----------
        method : str
            Kanboard API procedure name (camelCase).
        **params
            Parameters forwarded in the JSON body.

        Returns
        -------
        Any
            The ``result`` value from the API response.

        Raises
        ------
        RuntimeError
            When the API returns an ``error`` object.
        httpx.HTTPStatusError
            On HTTP-level failures (4xx, 5xx).
        """
        self._rpc_id += 1
        body: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._rpc_id,
            "params": params,
        }
        if self._client is None:
            raise RuntimeError("Not connected — call connect() first")
        try:
            response = await self._client.post(self._jsonrpc_url, json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Kanboard API HTTP error (%s) for method '%s': %s",
                exc.response.status_code,
                method,
                exc.response.text[:300],
            )
            raise

        data = response.json()
        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            raise RuntimeError(f"Kanboard API error in {method}: {msg}")
        return data.get("result")

    async def _refresh_columns(self) -> None:
        """
        Fetch the project's column list and populate the lookup maps.

        Called once during ``connect()`` and can be called again if the
        board layout changes at runtime.
        """
        columns = await self._rpc("getColumns", project_id=self._project_id)
        self._column_map = {}
        self._column_status_map = {}
        for col in columns or []:
            name = col.get("title", "")
            raw_id = col.get("id")
            if raw_id is None:
                logger.warning("Kanboard returned column with null id; skipping: %s", col)
                continue
            cid = int(raw_id)
            self._column_map[name.lower()] = cid
            self._column_status_map[cid] = _COLUMN_STATUS_MAP.get(
                name.lower(), TaskStatus.TODO
            )
        logger.debug(
            "Kanboard columns cached: %s",
            {k: v for k, v in self._column_map.items()},
        )

    async def _resolve_user_id(self, assignee_id: str) -> Optional[int]:
        """
        Resolve a Marcus assignee identifier to a Kanboard user ID.

        Tries numeric parse first, then username lookup.

        Parameters
        ----------
        assignee_id : str
            Numeric user ID or Kanboard username.

        Returns
        -------
        Optional[int]
            Kanboard user ID, or ``None`` if no match found.
        """
        try:
            return int(assignee_id)
        except (TypeError, ValueError):
            pass

        try:
            user = await self._rpc("getUserByName", username=assignee_id)
            if user:
                return int(user.get("id", 0)) or None
        except Exception:
            pass

        return None

    def _to_task(self, raw: Dict[str, Any]) -> Task:
        """
        Convert a raw Kanboard task dict to a Marcus ``Task``.

        Parameters
        ----------
        raw : Dict[str, Any]
            Single task object from the Kanboard JSON-RPC API.

        Returns
        -------
        Task
            Normalised ``Task`` understood by all Marcus components.
        """
        column_id = int(raw.get("column_id") or 0)
        column_name = raw.get("column_name") or ""

        # Prefer column_name if provided; fall back to id-based lookup
        if column_name:
            status = self.normalize_status(column_name)
        else:
            status = self._column_status_map.get(column_id, TaskStatus.TODO)
            # Respect Kanboard's is_active flag as a safety net
            if int(raw.get("is_active", 1)) == 0:
                status = TaskStatus.DONE

        now = datetime.now(timezone.utc)
        created_at = _parse_kanboard_ts(raw.get("date_creation")) or now
        updated_at = _parse_kanboard_ts(raw.get("date_modification")) or now
        due_date = _parse_kanboard_ts(raw.get("date_due"))

        # time_estimated is stored in seconds by Kanboard
        estimated_seconds = int(raw.get("time_estimated") or 0)
        estimated_hours = estimated_seconds / 3600.0 if estimated_seconds else 0.0

        assignee = raw.get("owner_id")
        assigned_to = str(assignee) if assignee and int(assignee) != 0 else None

        labels: List[str] = []
        if raw.get("tags"):
            labels = [t.get("name", "") for t in raw["tags"] if t.get("name")]

        return Task(
            id=str(raw.get("id", "")),
            name=raw.get("title", ""),
            description=raw.get("description", "") or "",
            status=status,
            priority=self.normalize_priority(raw.get("priority", 0)),
            assigned_to=assigned_to,
            created_at=created_at,
            updated_at=updated_at,
            due_date=due_date,
            project_id=str(raw.get("project_id", self._project_id)),
            project_name=self._project_name,
            labels=labels,
            estimated_hours=estimated_hours,
        )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _parse_kanboard_ts(value: Any) -> Optional[datetime]:
    """
    Convert a Kanboard Unix timestamp to a timezone-aware ``datetime``.

    Kanboard stores most dates as Unix epoch integers (or ``"0"`` for
    unset dates).  Returns ``None`` for absent or zero values.

    Parameters
    ----------
    value : Any
        Unix timestamp from the Kanboard API (int, str, or ``None``).

    Returns
    -------
    Optional[datetime]
        UTC-aware ``datetime``, or ``None``.
    """
    if not value:
        return None
    try:
        ts = int(value)
        if ts == 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _marcus_priority_to_kb(priority: Any) -> int:
    """
    Convert a Marcus priority string or enum to a Kanboard integer (0–3).

    Parameters
    ----------
    priority : Any
        Marcus priority value (``Priority`` enum, string, or ``None``).

    Returns
    -------
    int
        Kanboard priority (0 = low … 3 = urgent).
    """
    name = str(priority).lower()
    if "urgent" in name or "critical" in name:
        return 3
    if "high" in name:
        return 2
    if "low" in name:
        return 0
    return 1  # MEDIUM default
