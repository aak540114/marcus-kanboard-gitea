"""
Guard: both dev-environment preview widgets (the per-ticket sidebar panel
and the project-level main-branch preview widget in the board header)
poll their status endpoint instead of checking once on page load.

Background: "Start Preview" / "Start Main Preview" is a plain
<a target="_blank"> navigation to a NEW tab — starting a preview never
touches Kanboard ticket/board state, so it never fires the EventSource
"refresh" push the rest of the board relies on for live updates, and the
tab that has the Start button gets no click event or other signal at
all. Before this fix, both widgets checked status exactly once on load,
so a human had to manually reload the page to see "Open Preview" /
"View Logs" / "Stop Preview" appear after starting one. Reported
directly by the user for the main-branch preview widget; the per-ticket
sidebar panel had the identical bug (same one-shot pattern, same
now-incorrect comment claiming the EventSource block already handles it).

There is no live-browser harness for this plugin — this is a cheap
static regression guard, same approach as
test_board_header_project_stats.py / test_sidebar_dev_env_logs_button.py.
"""

from pathlib import Path

SIDEBAR = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/task/sidebar.php"
)
HEADER = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/board/header.php"
)


class TestSidebarDevEnvPanelPolls:
    def test_polls_on_an_interval_not_just_once(self):
        src = SIDEBAR.read_text()
        assert "setInterval(checkDevEnvStatus" in src

    def test_poll_decision_is_driven_by_a_dedicated_check_function(self):
        src = SIDEBAR.read_text()
        assert "function checkDevEnvStatus" in src

    def test_a_stop_click_holds_the_poll_off_until_it_resolves(self):
        """Otherwise a poll landing mid-stop could see the container
        still technically running and flip the UI back to "running"
        right under the "Stopping…" button."""
        src = SIDEBAR.read_text()
        idx = src.index("function checkDevEnvStatus")
        block = src[idx : idx + 200]
        assert "'stopping'" in block

    def test_unchanged_status_does_not_force_a_rerender(self):
        """Dedup guard — an identical poll result must not tear down and
        rebuild the DOM (flicker, lost focus) every 4 seconds."""
        src = SIDEBAR.read_text()
        idx = src.index("function checkDevEnvStatus")
        block = src[idx : idx + 1200]
        assert "newState === devEnvLastState" in block

    def test_stopping_guard_is_rechecked_after_the_fetch_resolves(self):
        """Regression: checking 'stopping' only BEFORE issuing the fetch
        is not enough — a poll already in flight when Stop is clicked
        resolves with stale running:true data after devEnvLastState has
        since moved to 'stopping', which used to slip past the (now
        mismatched) newState-vs-devEnvLastState check and call
        renderRunning(), silently replacing the "Stopping…" button with
        a fresh, enabled one mid-request. Confirmed by an isolated JS
        repro before this fix existed. The guard must appear a SECOND
        time, inside the .then(function (data) {...}) callback itself —
        not just once, before the fetch call."""
        src = SIDEBAR.read_text()
        idx = src.index("function checkDevEnvStatus")
        block = src[idx : idx + 1200]
        assert block.count("devEnvLastState === 'stopping'") >= 2


class TestMainPreviewWidgetPolls:
    def test_polls_on_an_interval_not_just_once(self):
        src = HEADER.read_text()
        assert "setInterval(checkStatus" in src

    def test_poll_decision_is_driven_by_a_dedicated_check_function(self):
        src = HEADER.read_text()
        assert "function checkStatus" in src

    def test_a_stop_click_holds_the_poll_off_until_it_resolves(self):
        src = HEADER.read_text()
        idx = src.index("function checkStatus")
        block = src[idx : idx + 200]
        assert "'stopping'" in block

    def test_unchanged_status_does_not_force_a_rerender(self):
        src = HEADER.read_text()
        idx = src.index("function checkStatus")
        block = src[idx : idx + 1200]
        assert "newState === lastState" in block

    def test_stopping_guard_is_rechecked_after_the_fetch_resolves(self):
        """Regression: same race as the sidebar panel's checkDevEnvStatus
        — see its identical test for the full trace. The guard must
        appear a SECOND time, inside the .then(function (data) {...})
        callback, not just once before the fetch call."""
        src = HEADER.read_text()
        idx = src.index("function checkStatus")
        block = src[idx : idx + 1200]
        assert block.count("lastState === 'stopping'") >= 2
