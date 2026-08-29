"""
Unit tests for the ``/dev-env/logs`` docker-logs viewer page.

Background: a dev-environment preview container can be fully "up" (port
accepting connections) while showing 404s or the wrong content, because
its real dev command failed inside the entrypoint script and it silently
degraded to serving the raw repo as static files instead — nothing about
that failure is visible from the preview URL itself. This page polls
``/api/dev-env/logs`` (fresh ``docker logs`` output) so a human can see
exactly what happened. These tests verify the page's contract.
"""

import json

from src.marcus_mcp.server import _dev_env_logs_page


class TestDevEnvLogsPage:
    def test_polls_the_logs_api_endpoint(self) -> None:
        page = _dev_env_logs_page("7", "kanboard")
        assert "/api/dev-env/logs" in page

    def test_embeds_ticket_and_provider_as_json(self) -> None:
        page = _dev_env_logs_page("7", "kanboard")
        assert json.dumps("7") in page
        assert json.dumps("kanboard") in page

    def test_is_a_complete_html_document(self) -> None:
        page = _dev_env_logs_page("7", "kanboard")
        assert page.lstrip().startswith("<!doctype html>")
        assert "</html>" in page

    def test_escapes_ticket_id_in_markup(self) -> None:
        page = _dev_env_logs_page("a<b", "kanboard")
        assert "a<b</h1>" not in page
        assert "a&lt;b" in page

    def test_auto_refreshes_via_polling(self) -> None:
        """The page must poll on an interval, not just fetch once — the
        whole point is watching logs update as the container runs."""
        page = _dev_env_logs_page("7", "kanboard")
        assert "setInterval" in page

    def test_no_token_by_default(self) -> None:
        """No token given -> the poll URL never appends &token= (an
        empty token in the JS string must not leak a literal '&token='
        with nothing after it)."""
        page = _dev_env_logs_page("7", "kanboard")
        assert json.dumps("") in page

    def test_embeds_token_for_authenticated_polling(self) -> None:
        """A token IS given -> embedded as a JS literal so this
        standalone page's own poll() calls can authenticate against a
        token-protected Marcus (this page has no other way to attach the
        bearer header a freshly opened tab never received)."""
        page = _dev_env_logs_page("7", "kanboard", token="secret123")
        assert json.dumps("secret123") in page

    def test_token_is_not_leaked_into_visible_markup(self) -> None:
        """The token is only ever used inside the <script> block as a JS
        string literal — never rendered as visible page text/markup."""
        page = _dev_env_logs_page("7", "kanboard", token="secret123")
        # It appears exactly once: inside the JS var assignment.
        assert page.count("secret123") == 1

    def test_stops_polling_once_the_container_is_not_running(self) -> None:
        """Regression: the page used to poll forever via a single
        unconditional setInterval, even after the human clicked Stop
        Preview — so it kept hammering the API and showing a "container
        not running" pill indefinitely. Polling must stop once running
        is observed false, and only resume if running becomes true again
        (e.g. a new preview was started for the same ticket)."""
        page = _dev_env_logs_page("7", "kanboard")
        assert "function stopPolling" in page
        assert "function startPolling" in page
        assert "clearInterval" in page
        # The decision to start/stop lives in refresh()'s own callback,
        # driven by data.running — not a single unconditional interval
        # set up once at page load.
        assert "if (data.running) { startPolling(); } else { stopPolling(); }" in page
