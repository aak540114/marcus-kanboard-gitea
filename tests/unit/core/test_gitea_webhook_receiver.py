"""
Unit tests for GiteaWebhookReceiver.

Verifies HMAC signature validation, branch-to-ticket parsing, and that a
refresh is triggered on the DevEnvironmentManager for matching pushes.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.gitea_webhook_receiver import GiteaWebhookReceiver


@pytest.fixture
def mock_dev_env():
    """Mock DevEnvironmentManager exposing an async refresh()."""
    mgr = MagicMock()
    mgr.refresh = AsyncMock(return_value=True)
    return mgr


@pytest.fixture
def receiver(mock_dev_env):
    """Receiver with no secret configured (signature validation off)."""
    return GiteaWebhookReceiver(dev_env_manager=mock_dev_env)


@pytest.fixture
def secured_receiver(mock_dev_env):
    """Receiver with a required HMAC secret."""
    return GiteaWebhookReceiver(dev_env_manager=mock_dev_env, secret="s3cret")


def _push_body(branch: str, repo_name: str = "shopping-cart") -> bytes:
    """Build a Gitea push-webhook payload as raw bytes."""
    return json.dumps(
        {
            "ref": f"refs/heads/{branch}",
            "repository": {"name": repo_name},
        }
    ).encode()


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------


class TestSignatureValidation:
    @pytest.mark.asyncio
    async def test_no_secret_accepts_any_request(self, receiver):
        body = _push_body("ticket/kanboard/42")
        result = await receiver.handle_request(body)
        assert result is True

    @pytest.mark.asyncio
    async def test_correct_signature_accepted(self, secured_receiver):
        body = _push_body("ticket/kanboard/42")
        sig = _sign("s3cret", body)
        result = await secured_receiver.handle_request(body, signature=sig)
        assert result is True

    @pytest.mark.asyncio
    async def test_missing_signature_rejected(self, secured_receiver, mock_dev_env):
        body = _push_body("ticket/kanboard/42")
        result = await secured_receiver.handle_request(body, signature=None)
        assert result is False
        mock_dev_env.refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_signature_rejected(self, secured_receiver, mock_dev_env):
        body = _push_body("ticket/kanboard/42")
        result = await secured_receiver.handle_request(body, signature="deadbeef")
        assert result is False
        mock_dev_env.refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_signature_computed_over_wrong_body_rejected(self, secured_receiver):
        body = _push_body("ticket/kanboard/42")
        sig = _sign("s3cret", b'{"different": "body"}')
        result = await secured_receiver.handle_request(body, signature=sig)
        assert result is False


# ---------------------------------------------------------------------------
# Malformed payloads
# ---------------------------------------------------------------------------


class TestMalformedPayload:
    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self, receiver, mock_dev_env):
        result = await receiver.handle_request(b"NOT JSON {{{")
        assert result is False
        mock_dev_env.refresh.assert_not_called()


# ---------------------------------------------------------------------------
# Branch parsing + refresh triggering
# ---------------------------------------------------------------------------


class TestRefreshTriggering:
    @pytest.mark.asyncio
    async def test_ticket_branch_triggers_refresh(self, receiver, mock_dev_env):
        body = _push_body("ticket/kanboard/42")
        result = await receiver.handle_request(body)
        assert result is True
        mock_dev_env.refresh.assert_awaited_once_with("42", "kanboard")

    @pytest.mark.asyncio
    async def test_ticket_id_with_slashes_preserved(self, receiver, mock_dev_env):
        """Ticket ids/branches can contain further path segments — only the
        first two slash-delimited components are provider/ticket_id."""
        body = _push_body("ticket/github/org-repo-42")
        await receiver.handle_request(body)
        mock_dev_env.refresh.assert_awaited_once_with("org-repo-42", "github")

    @pytest.mark.asyncio
    async def test_non_ticket_branch_ignored(self, receiver, mock_dev_env):
        body = _push_body("main")
        result = await receiver.handle_request(body)
        assert result is True
        mock_dev_env.refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_tag_push_ignored(self, receiver, mock_dev_env):
        body = json.dumps(
            {"ref": "refs/tags/v1.0.0", "repository": {"name": "app"}}
        ).encode()
        result = await receiver.handle_request(body)
        assert result is True
        mock_dev_env.refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_true_when_no_env_running_for_ticket(
        self, receiver, mock_dev_env
    ):
        """A well-formed ticket-branch push with nothing to refresh is
        still a successfully processed delivery, not an error."""
        mock_dev_env.refresh = AsyncMock(return_value=False)
        body = _push_body("ticket/kanboard/99")
        result = await receiver.handle_request(body)
        assert result is True

    @pytest.mark.asyncio
    async def test_refresh_exception_returns_false(self, receiver, mock_dev_env):
        mock_dev_env.refresh = AsyncMock(side_effect=RuntimeError("docker exec failed"))
        body = _push_body("ticket/kanboard/42")
        result = await receiver.handle_request(body)
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_repository_key_does_not_crash(self, receiver, mock_dev_env):
        body = json.dumps({"ref": "refs/heads/ticket/kanboard/1"}).encode()
        result = await receiver.handle_request(body)
        assert result is True
        mock_dev_env.refresh.assert_awaited_once_with("1", "kanboard")
