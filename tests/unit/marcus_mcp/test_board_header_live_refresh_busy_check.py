"""
Guard: the MarcusDevEnv board-header template's live-refresh "is the
human busy" check actually detects an open Kanboard modal (the "Add a
new task" dialog, editing a ticket, bulk actions, confirm dialogs — all
of it), instead of silently never matching.

Regression: the check used to test for "#popover-container, .modal-box".
Neither selector can ever match a real Kanboard v1.2.53 page —
#popover-container doesn't correspond to anything Kanboard's own JS
creates (verified directly against its compiled assets/js/app.min.js:
zero occurrences of the string "popover" anywhere in it), and
.modal-box is a CLASS selector when Kanboard's actual modal system
(KB.modal's create()/destroy() functions, same file) builds an ID —
id="modal-box" nested inside id="modal-overlay", both appended to
document.body for the modal's entire lifetime and removed together on
close. So a live refresh only got deferred when the human's focus
happened to be literally inside a text field at that exact instant — an
open "new task" dialog with focus anywhere else (a dropdown just
clicked, a pause between keystrokes) got silently wiped by
window.location.reload() the moment Marcus pushed any change, which is
frequent while an agent is actively working (progress comments land
roughly every 10s).

There is no live-browser harness for this plugin — this is a cheap
static regression guard, same approach as the sibling header.php tests.
"""

from pathlib import Path

HEADER = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/board/header.php"
)


def _live_refresh_block() -> str:
    src = HEADER.read_text()
    idx = src.index("Live board refresh (push, no polling)")
    # Generous window: covers userIsBusy(), doRefresh(), and the
    # deferred-retry setInterval that both read it.
    return src[idx : idx + 3000]


def test_busy_check_uses_the_real_kanboard_modal_selector():
    block = _live_refresh_block()
    assert "document.querySelector('#modal-overlay')" in block


def test_busy_check_no_longer_uses_the_dead_selectors():
    """Regression: neither old selector ever matched anything real — if
    either creeps back into the ACTUAL querySelector call, the
    underlying bug is back too. (The strings may still legitimately
    appear in this file's own explanatory comment about the history —
    only the live call site matters here.)"""
    block = _live_refresh_block()
    assert "document.querySelector('#popover-container" not in block
    assert "querySelector('#popover-container, .modal-box')" not in block


def test_text_field_focus_still_counts_as_busy():
    """The text-field check (typing a comment, a title, etc.) is a
    separate, independently-useful signal — must not be removed while
    fixing the modal-detection selector."""
    block = _live_refresh_block()
    assert "TEXTAREA" in block
    assert "isContentEditable" in block


def test_deferred_refresh_is_retried_once_no_longer_busy():
    """The retry loop (setInterval) must still exist and still consult
    the same busy check, so a refresh deferred by an open dialog is
    applied once the dialog is actually closed — not dropped forever."""
    block = _live_refresh_block()
    assert "setInterval" in block
    assert "pending && !userIsBusy()" in block
