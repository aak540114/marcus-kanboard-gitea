"""
Guard: the MarcusDevEnv task-sidebar template links to the new
"/dev-env/logs" docker-logs viewer (see src/marcus_mcp/server.py's
dev_env_logs_view / dev_env_logs_api and
DevEnvironmentManager.get_live_logs).

There is no live-browser harness for this plugin — this is a cheap
static regression guard, same approach as
test_board_header_project_stats.py / test_board_header_clone_project.py.
"""

from pathlib import Path

SIDEBAR = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/task/sidebar.php"
)


def test_logs_url_wired_from_php_config():
    src = SIDEBAR.read_text()
    assert "$logsUrl" in src
    assert "/dev-env/logs" in src


def test_logs_url_carries_ticket_id_and_provider():
    src = SIDEBAR.read_text()
    idx = src.index("$logsUrl")
    block = src[idx : idx + 300]
    assert "ticket_id=" in block
    assert "provider=" in block


def test_logs_url_carries_marcus_token_when_set():
    """Same auth mechanism as $viewUrl/$stopUrl — a plain <a href> can't
    carry a bearer header, so the token rides in the query string when
    Marcus requires it (remote-access mode)."""
    src = SIDEBAR.read_text()
    idx = src.index("$logsUrl")
    block = src[idx : idx + 300]
    assert "marcusToken" in block


def test_logs_js_var_is_wired():
    src = SIDEBAR.read_text()
    assert "var LOGS_URL" in src
    assert "json_encode($logsUrl)" in src


def test_view_logs_button_present_in_running_state():
    src = SIDEBAR.read_text()
    idx = src.index("function renderRunning")
    block = src[idx : idx + 800]
    assert "View Logs" in block
    assert "LOGS_URL" in block


def test_view_logs_button_opens_in_new_tab():
    src = SIDEBAR.read_text()
    idx = src.index("marcus-logs-btn")
    block = src[max(0, idx - 200) : idx + 50]
    assert 'target="_blank"' in block
    assert 'rel="noopener noreferrer"' in block
