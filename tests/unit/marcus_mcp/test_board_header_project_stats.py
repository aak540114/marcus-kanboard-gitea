"""
Guard: the MarcusDevEnv board-header template links to the new
"Project Stats" page (see src/marcus_mcp/server.py's project_stats_page /
project_stats_api and src/core/project_stats.py).

There is no live-browser harness for this plugin — this is a cheap
static regression guard, same approach as
test_board_header_clone_project.py / test_board_header_escaping.py.
"""

from pathlib import Path

HEADER = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/board/header.php"
)


def test_stats_url_wired_from_php_config():
    src = HEADER.read_text()
    assert "$statsUrl" in src
    assert "/project-stats?project_id=" in src


def test_stats_link_present_and_escaped():
    src = HEADER.read_text()
    assert "Project Stats" in src
    # Same escaping convention as the sibling Project Description link —
    # a server-controlled URL still goes through htmlspecialchars().
    assert 'href="<?= htmlspecialchars($statsUrl) ?>"' in src


def test_stats_link_opens_in_new_tab():
    src = HEADER.read_text()
    # Locate the Project Stats anchor block specifically.
    idx = src.index("<!-- Project Stats link -->")
    block = src[idx : idx + 400]
    assert 'target="_blank"' in block
    assert 'rel="noopener noreferrer"' in block
