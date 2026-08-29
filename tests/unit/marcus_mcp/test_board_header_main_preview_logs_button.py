"""
Guard: the MarcusDevEnv board-header template links to "/dev-env/logs"
for the project's MAIN-BRANCH preview too, not just the per-ticket
sidebar preview (which already has its own "View Logs" button — see
test_sidebar_dev_env_logs_button.py).

There is no live-browser harness for this plugin — this is a cheap
static regression guard, same approach as
test_board_header_project_stats.py / test_board_header_clone_project.py.
"""

from pathlib import Path

HEADER = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/board/header.php"
)


def test_main_logs_url_wired_from_php_config():
    src = HEADER.read_text()
    assert "$devEnvMainLogsUrl" in src
    assert "/dev-env/logs" in src


def test_main_logs_url_uses_the_synthetic_main_ticket_id():
    """The main-branch preview's DevEnvironmentManager identity is
    "main-<project_id>" (see _main_preview_ticket_id in server.py) —
    /dev-env/logs is generic over ticket_id, so this is the only wiring
    needed, no dedicated route."""
    src = HEADER.read_text()
    idx = src.index("$devEnvMainLogsUrl")
    block = src[idx : idx + 300]
    assert "'main-'" in block
    assert "projectId" in block


def test_main_logs_url_carries_marcus_token_when_set():
    src = HEADER.read_text()
    idx = src.index("$devEnvMainLogsUrl")
    block = src[idx : idx + 300]
    assert "marcusToken" in block


def test_main_logs_js_var_is_wired():
    src = HEADER.read_text()
    assert "var DEV_ENV_MAIN_LOGS_URL" in src
    assert "json_encode($devEnvMainLogsUrl)" in src


def test_view_logs_button_present_in_running_state():
    src = HEADER.read_text()
    idx = src.index("function renderRunning")
    block = src[idx : idx + 800]
    assert "View Logs" in block
    assert "DEV_ENV_MAIN_LOGS_URL" in block


def test_view_logs_button_opens_in_new_tab():
    src = HEADER.read_text()
    idx = src.index("marcus-main-logs-btn")
    block = src[max(0, idx - 250) : idx + 50]
    assert 'target="_blank"' in block
    assert 'rel="noopener noreferrer"' in block
