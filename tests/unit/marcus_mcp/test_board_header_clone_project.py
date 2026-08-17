"""
Guard: the MarcusDevEnv board-header template's "Clone this project"
button wires up correctly and escapes server-controlled values before
injecting them into innerHTML (the job's new_project_id and error message
come back from Marcus's /api/clone-project-status, not a fixed constant,
so a crafted project name reflected into an error string is a stored-XSS
vector the same way the agent-usage fields were — see
test_board_header_escaping.py).

There is no JS test harness for this plugin (no live Kanboard/browser in
this environment) — this is a cheap static regression guard, same
approach as the sibling escaping test.
"""

from pathlib import Path

HEADER = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/board/header.php"
)


def test_clone_button_and_status_span_present():
    src = HEADER.read_text()
    assert 'id="marcus-clone-project-btn"' in src
    assert 'onclick="cloneThisProject()"' in src
    assert 'id="marcus-clone-status"' in src


def test_clone_urls_wired_from_php_config():
    src = HEADER.read_text()
    assert "$cloneProjectUrl" in src
    assert "$cloneProjectStatusUrl" in src
    assert "CLONE_PROJECT_URL" in src
    assert "CLONE_PROJECT_STATUS_URL" in src


def test_clone_handler_posts_baseline_id_and_new_name():
    src = HEADER.read_text()
    assert "baseline_project_id: PROJECT_ID" in src
    assert "new_name: name" in src


def test_clone_status_fields_are_escaped():
    src = HEADER.read_text()
    assert "mEsc(data.new_project_id)" in src
    assert "mEsc(data.error" in src


def test_no_unescaped_error_interpolation():
    src = HEADER.read_text()
    # The vulnerable form would concatenate data.error/new_project_id raw
    # into innerHTML/textContent without mEsc().
    assert "+ data.error +" not in src
    assert "+ data.new_project_id +" not in src


def test_polling_has_a_bounded_attempt_cap():
    """A stuck 'running' status must not poll forever."""
    src = HEADER.read_text()
    assert "MAX_POLL_ATTEMPTS" in src


def test_clone_complete_message_links_to_the_new_project():
    """Regression: 'Clone complete (project #N)' used to be plain text
    with no way to actually reach the new project, easy to miss (11px
    gray status text) and easy to mistake for "no project was created".
    The success branch must render a clickable link to the new project's
    board, built from window.location.origin (this script only ever runs
    inside a Kanboard-served page, so that origin is always Kanboard's
    own — no separate config needed) — Kanboard's board route is verified
    as board/:project_id -> BoardViewController::show.
    """
    src = HEADER.read_text()
    idx = src.index("if (data.status === 'done')")
    block = src[idx : idx + 900]
    assert "window.location.origin + '/board/'" in block
    assert "mEsc(boardUrl)" in block
    assert '<a href=' in block
    assert 'target="_blank"' in block
    assert 'rel="noopener noreferrer"' in block
