"""
Guard: the MarcusDevEnv board-header template's "Audit" button wires up
correctly — the create call, the CURRENT_USER_ID auto-assignment, and the
open/disable polling against /api/audit-status (only one audit ticket may
be open per project at a time).

There is no JS test harness for this plugin (no live Kanboard/browser in
this environment) — this is a cheap static regression guard, same
approach as the sibling "Clone this project" test
(test_board_header_clone_project.py).
"""

from pathlib import Path

HEADER = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/board/header.php"
)


def test_audit_button_and_status_span_present():
    src = HEADER.read_text()
    assert 'id="marcus-audit-btn"' in src
    assert 'onclick="startCodebaseAudit()"' in src
    assert 'id="marcus-audit-status"' in src


def test_audit_urls_wired_from_php_config():
    src = HEADER.read_text()
    assert "$auditProjectUrl" in src
    assert "$auditStatusUrl" in src
    assert "AUDIT_PROJECT_URL" in src
    assert "AUDIT_STATUS_URL" in src


def test_current_user_id_embedded_via_kanboard_user_helper():
    """The created ticket must be auto-owned by whoever clicked the
    button (not left unassigned) — this is the verified real Kanboard API
    (Template::__get -> UserHelper::getId -> UserSession::getId) for
    reading the logged-in user's id inside a template."""
    src = HEADER.read_text()
    assert "CURRENT_USER_ID" in src
    assert "$this->user->getId()" in src


def test_audit_handler_posts_project_id_and_requested_by():
    src = HEADER.read_text()
    assert "project_id: PROJECT_ID" in src
    assert "requested_by: CURRENT_USER_ID" in src


def test_audit_button_disables_while_one_is_already_open():
    """Decision: disable the button while a project already has an audit
    ticket open, re-enable once it's Done — /api/audit-status is the
    source of truth (HumanGatedWorkflow.has_open_audit_ticket)."""
    src = HEADER.read_text()
    idx = src.index("function refreshAuditButtonState")
    block = src[idx : idx + 700]
    assert "data.has_open_audit" in block
    assert "btn.disabled = true" in block
    assert "btn.disabled = false" in block


def test_audit_status_polled_on_load_and_periodically():
    src = HEADER.read_text()
    idx = src.index("function refreshAuditButtonState")
    block = src[idx:idx + 900]
    assert "refreshAuditButtonState();" in block
    assert "setInterval(refreshAuditButtonState, INTERVAL);" in block


def test_audit_error_and_ticket_id_messages_are_escaped():
    src = HEADER.read_text()
    idx = src.index("window.startCodebaseAudit")
    block = src[idx : idx + 1200]
    assert "mEsc(data.error" in block
    assert "mEsc(data.ticket_id)" in block


def test_no_unescaped_error_interpolation():
    src = HEADER.read_text()
    idx = src.index("window.startCodebaseAudit")
    block = src[idx : idx + 1200]
    assert "+ data.error +" not in block
    assert "+ data.ticket_id +" not in block


def test_start_audit_disables_button_immediately_on_click():
    """Prevents a double-click from firing two POSTs before the first
    /api/audit-status re-check comes back."""
    src = HEADER.read_text()
    idx = src.index("window.startCodebaseAudit")
    block = src[idx : idx + 300]
    assert "btn.disabled = true;" in block
