"""
Unit tests for scripts/provision_kanboard.py

All Kanboard JSON-RPC calls go through ``urllib.request.urlopen`` — every
test mocks that single seam, no real HTTP or Kanboard instance involved.
"""

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))

import provision_kanboard as pk  # noqa: E402


def _rpc_response(result) -> MagicMock:
    """Build a mock urlopen() context manager returning a JSON-RPC result."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
    resp = MagicMock()
    resp.read = MagicMock(return_value=body)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _rpc_error(message: str) -> MagicMock:
    """Build a mock urlopen() context manager returning a JSON-RPC error field."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": message}}
    ).encode()
    resp = MagicMock()
    resp.read = MagicMock(return_value=body)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _non_json_response() -> MagicMock:
    """Build a mock urlopen() returning a non-JSON body (e.g. a stray HTML page)."""
    resp = MagicMock()
    resp.read = MagicMock(return_value=b"<html>not json</html>")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestCallRpc:
    """call_rpc() — the single HTTP seam used by everything else."""

    def test_returns_result_on_success(self):
        """A clean JSON-RPC response returns its `result` field."""
        with patch("provision_kanboard.urllib.request.urlopen", return_value=_rpc_response(42)):
            result = pk.call_rpc("http://x/jsonrpc.php", "tok", "getVersion")
        assert result == 42

    def test_sends_basic_auth_as_jsonrpc_user(self):
        """Auth is HTTP Basic as user 'jsonrpc' with the token as password."""
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return _rpc_response("ok")

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            pk.call_rpc("http://x/jsonrpc.php", "sekret", "getVersion")

        import base64

        expected = "Basic " + base64.b64encode(b"jsonrpc:sekret").decode()
        assert captured["auth"] == expected

    def test_raises_auth_error_on_401_without_retry(self):
        """HTTP 401 raises immediately — a bad token won't fix itself by waiting."""
        call_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            call_count["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(pk.KanboardAuthError):
                pk.call_rpc("http://x/jsonrpc.php", "bad-tok", "getVersion", retries=5)

        assert call_count["n"] == 1  # no retry on auth failure

    def test_raises_auth_error_on_403(self):
        """HTTP 403 is treated the same as 401 — an auth failure, not retried."""
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(pk.KanboardAuthError):
                pk.call_rpc("http://x/jsonrpc.php", "bad-tok", "getVersion")

    def test_retries_connection_error_then_succeeds(self):
        """Transient connection errors are retried and a later success wins."""
        responses = iter(
            [
                urllib.error.URLError("connection refused"),
                urllib.error.URLError("connection refused"),
                _rpc_response("ok"),
            ]
        )

        def fake_urlopen(req, timeout=None):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen), patch(
            "provision_kanboard.time.sleep"
        ):
            result = pk.call_rpc("http://x/jsonrpc.php", "tok", "getVersion", retries=5, retry_delay=0)

        assert result == "ok"

    def test_gives_up_after_max_retries(self):
        """A connection error on every attempt exhausts retries and raises."""
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen), patch(
            "provision_kanboard.time.sleep"
        ):
            with pytest.raises(pk.KanboardRPCError):
                pk.call_rpc("http://x/jsonrpc.php", "tok", "getVersion", retries=3, retry_delay=0)

    def test_raises_rpc_error_on_error_field(self):
        """A JSON-RPC response with an `error` field raises KanboardRPCError."""
        with patch(
            "provision_kanboard.urllib.request.urlopen", return_value=_rpc_error("Invalid params")
        ):
            with pytest.raises(pk.KanboardRPCError):
                pk.call_rpc("http://x/jsonrpc.php", "tok", "createProject")

    def test_retries_non_json_response_then_succeeds(self):
        """A non-JSON body (e.g. a stray session/error page) is retried, not fatal.

        Regression test: json.JSONDecodeError previously wasn't caught by
        either except clause and propagated as a raw traceback, skipping
        the retry loop entirely.
        """
        responses = iter([_non_json_response(), _rpc_response("ok")])

        def fake_urlopen(req, timeout=None):
            return next(responses)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen), patch(
            "provision_kanboard.time.sleep"
        ):
            result = pk.call_rpc("http://x/jsonrpc.php", "tok", "getVersion", retries=3, retry_delay=0)

        assert result == "ok"

    def test_gives_up_after_max_retries_on_persistent_non_json(self):
        """A non-JSON body on every attempt exhausts retries and raises cleanly."""
        with patch(
            "provision_kanboard.urllib.request.urlopen", return_value=_non_json_response()
        ), patch("provision_kanboard.time.sleep"):
            with pytest.raises(pk.KanboardRPCError):
                pk.call_rpc("http://x/jsonrpc.php", "tok", "getVersion", retries=3, retry_delay=0)


class TestFindOrCreateProject:
    """find_or_create_project() — check-then-create, using createProject's
    own return value instead of a redundant re-fetch."""

    def test_returns_existing_project_id_without_creating(self):
        """An existing project is found and returned with a single RPC call."""
        responses = [_rpc_response({"id": "7", "name": "Marcus Project"})]

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses) as m:
            project_id = pk.find_or_create_project("http://x/jsonrpc.php", "tok", "Marcus Project")

        assert project_id == 7
        assert m.call_count == 1  # only getProjectByName — no createProject call

    def test_creates_project_when_missing(self):
        """A missing project is created; the id comes from createProject's
        own return value, not a second getProjectByName lookup."""
        responses = [
            _rpc_response(False),  # getProjectByName: not found
            _rpc_response(3),  # createProject returns the new int id directly
        ]

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses) as m:
            project_id = pk.find_or_create_project("http://x/jsonrpc.php", "tok", "Marcus Project")

        assert project_id == 3
        assert m.call_count == 2  # getProjectByName + createProject only

    def test_raises_if_create_returns_falsy(self):
        """createProject returning a falsy result (Kanboard's own failure
        signal) raises rather than silently returning a bad id."""
        responses = [
            _rpc_response(False),  # getProjectByName: not found
            _rpc_response(False),  # createProject failed
        ]

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses):
            with pytest.raises(pk.KanboardRPCError):
                pk.find_or_create_project("http://x/jsonrpc.php", "tok", "Marcus Project")


class TestEnsureAdminUser:
    """ensure_admin_user() — replace admin/admin since Kanboard's JSON-RPC
    API has no method to rotate an existing user's password."""

    def test_creates_user_and_disables_default_admin(self):
        """Fresh install: no marcus-admin user yet, default admin active."""
        responses = [
            _rpc_response(False),  # getUserByName(new username): not found
            _rpc_response(5),  # createUser: new user id
            _rpc_response({"id": "1", "username": "admin"}),  # getUserByName("admin")
            _rpc_response(True),  # disableUser
        ]
        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses) as m:
            changed = pk.ensure_admin_user(
                "http://x/jsonrpc.php", "tok", "marcus_admin", "s3cret"
            )
        assert changed is True
        assert m.call_count == 4

    def test_idempotent_when_already_secured(self):
        """Re-run: marcus-admin already exists, default admin already gone
        (getUserByName returns falsy) — no RPC calls that mutate state."""
        responses = [
            _rpc_response({"id": "5", "username": "marcus_admin"}),  # already exists
            _rpc_response(False),  # getUserByName("admin"): no longer present
        ]
        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses) as m:
            changed = pk.ensure_admin_user(
                "http://x/jsonrpc.php", "tok", "marcus_admin", "s3cret"
            )
        assert changed is False
        assert m.call_count == 2  # two lookups, no createUser/disableUser calls

    def test_disables_admin_even_when_marcus_user_already_exists(self):
        """User already created on a prior run, but default admin is still
        active (e.g. setup.sh was interrupted between the two steps) —
        must still disable it on this run."""
        responses = [
            _rpc_response({"id": "5", "username": "marcus_admin"}),
            _rpc_response({"id": "1", "username": "admin"}),
            _rpc_response(True),  # disableUser
        ]
        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses) as m:
            changed = pk.ensure_admin_user(
                "http://x/jsonrpc.php", "tok", "marcus_admin", "s3cret"
            )
        assert changed is True
        assert m.call_count == 3

    def test_never_disables_the_account_it_just_created(self):
        """Defensive check: if the new admin user happens to be named
        'admin' itself (unlikely given the generated-username scheme, but
        cheap to guard), never disable the account just created."""
        responses = [
            _rpc_response(False),  # getUserByName("admin"): not found yet
            _rpc_response(1),  # createUser returns id 1
            _rpc_response({"id": "1", "username": "admin"}),  # getUserByName("admin") now finds it
        ]
        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses) as m:
            changed = pk.ensure_admin_user("http://x/jsonrpc.php", "tok", "admin", "s3cret")
        assert changed is True  # created, but NOT disabled
        assert m.call_count == 3  # no disableUser call

    def test_raises_if_create_user_returns_falsy(self):
        """createUser returning a falsy result raises rather than silently
        reporting success while admin/admin is still the only login."""
        responses = [
            _rpc_response(False),  # getUserByName: not found
            _rpc_response(False),  # createUser failed
        ]
        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses):
            with pytest.raises(pk.KanboardRPCError):
                pk.ensure_admin_user("http://x/jsonrpc.php", "tok", "marcus_admin", "s3cret")


class TestReconcileColumns:
    """reconcile_columns() — idempotent rename-defaults + add-missing."""

    def test_renames_default_columns_and_adds_missing(self):
        """Fresh project: Backlog/Work in progress are renamed, the rest added."""
        columns = [
            {"id": "1", "title": "Backlog"},
            {"id": "2", "title": "Ready"},
            {"id": "3", "title": "Work in progress"},
            {"id": "4", "title": "Done"},
        ]
        calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            calls.append((body["method"], body["params"]))
            if body["method"] == "getColumns":
                return _rpc_response(columns)
            return _rpc_response(True)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            added = pk.reconcile_columns("http://x/jsonrpc.php", "tok", 1)

        assert ("updateColumn", ["1", "Todo"]) in calls
        assert ("updateColumn", ["3", "In Progress"]) in calls
        assert ("addColumn", [1, "Waiting for Human"]) in calls
        assert ("addColumn", [1, "Blocked"]) in calls
        assert set(added) == {"Waiting for Human", "Blocked"}
        # "Ready" and "Done" already matched — never touched
        assert not any(c[1] and c[1][0] == "2" for c in calls if c[0] == "updateColumn")

    def test_idempotent_noop_when_all_required_columns_present(self):
        """Re-running against an already-reconciled project makes zero calls."""
        columns = [
            {"id": "1", "title": "Todo"},
            {"id": "2", "title": "Ready"},
            {"id": "3", "title": "In Progress"},
            {"id": "4", "title": "Waiting for Human"},
            {"id": "5", "title": "Blocked"},
            {"id": "6", "title": "Done"},
        ]
        calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            calls.append(body["method"])
            if body["method"] == "getColumns":
                return _rpc_response(columns)
            return _rpc_response(True)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            added = pk.reconcile_columns("http://x/jsonrpc.php", "tok", 1)

        assert added == []
        assert "updateColumn" not in calls
        assert "addColumn" not in calls

    def test_skips_rename_when_target_already_exists(self):
        """Partial re-run: 'Todo' already added manually, 'Backlog' still there too."""
        columns = [
            {"id": "1", "title": "Backlog"},
            {"id": "2", "title": "Todo"},
            {"id": "3", "title": "Ready"},
            {"id": "4", "title": "Done"},
        ]
        calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            calls.append((body["method"], body["params"]))
            if body["method"] == "getColumns":
                return _rpc_response(columns)
            return _rpc_response(True)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            pk.reconcile_columns("http://x/jsonrpc.php", "tok", 1)

        # Must not try to rename Backlog->Todo again since Todo exists
        assert not any(m == "updateColumn" and params[1] == "Todo" for m, params in calls)

    def test_only_renames_present_defaults(self):
        """Only 'Backlog' present (not 'Work in progress') — one rename only."""
        columns = [
            {"id": "1", "title": "Backlog"},
            {"id": "2", "title": "Ready"},
            {"id": "3", "title": "Done"},
        ]
        calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            calls.append((body["method"], body["params"]))
            if body["method"] == "getColumns":
                return _rpc_response(columns)
            return _rpc_response(True)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            pk.reconcile_columns("http://x/jsonrpc.php", "tok", 1)

        update_calls = [c for c in calls if c[0] == "updateColumn"]
        assert len(update_calls) == 1
        assert update_calls[0][1] == ["1", "Todo"]


class TestMain:
    """main() — CLI entry point: stdout hygiene and exit codes."""

    def test_prints_project_id_and_returns_zero_on_success(self, capsys):
        """On success, stdout is exactly the bare project id (nothing else)."""
        with patch("provision_kanboard.find_or_create_project", return_value=5), patch(
            "provision_kanboard.reconcile_columns", return_value=[]
        ):
            rc = pk.main(["--url", "http://x/jsonrpc.php", "--token", "tok", "--project-name", "P"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == "5"

    def test_returns_one_on_auth_error(self, capsys):
        """An auth error exits 1 with the message on stderr, not stdout."""
        with patch(
            "provision_kanboard.find_or_create_project",
            side_effect=pk.KanboardAuthError("bad token"),
        ):
            rc = pk.main(["--url", "http://x/jsonrpc.php", "--token", "bad", "--project-name", "P"])

        assert rc == 1
        assert "bad token" in capsys.readouterr().err

    def test_returns_one_on_rpc_error(self, capsys):
        """A generic RPC error (e.g. exhausted retries) exits 1."""
        with patch(
            "provision_kanboard.find_or_create_project",
            side_effect=pk.KanboardRPCError("connection failed"),
        ):
            rc = pk.main(["--url", "http://x/jsonrpc.php", "--token", "tok", "--project-name", "P"])

        assert rc == 1
