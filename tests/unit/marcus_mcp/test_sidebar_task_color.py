"""
Guard: the MarcusDevEnv task-sidebar's card-color swatch picker wires up
correctly — lets a human change a ticket's Kanboard card color after
creation with one click, instead of needing the full "Edit task" form
(Kanboard has no other quick way to do this).

There is no JS test harness for this plugin (no live Kanboard/browser in
this environment) — this is a cheap static regression guard, same
approach as the sibling escaping/wiring tests (e.g.
test_board_header_audit.py).
"""

from pathlib import Path

SIDEBAR = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/task/sidebar.php"
)


def test_color_swatches_container_and_saving_indicator_present():
    src = SIDEBAR.read_text()
    assert 'id="marcus-color-swatches"' in src
    assert 'id="marcus-color-saving"' in src


def test_task_color_url_and_current_color_wired_from_php():
    src = SIDEBAR.read_text()
    assert "$taskColorUrl" in src
    assert "$currentColorId" in src
    assert "TASK_COLOR_URL" in src
    assert "CURRENT_COLOR_ID" in src


def test_current_color_defaults_from_the_task_row():
    src = SIDEBAR.read_text()
    assert "$task['color_id']" in src


def test_swatch_palette_matches_kanboards_fixed_color_ids():
    """Regression: a swatch for a color_id Kanboard doesn't recognize
    would silently no-op server-side (task_color_api validates against
    the same fixed list) — the two lists must stay in sync."""
    src = SIDEBAR.read_text()
    for color_id in (
        "yellow", "blue", "green", "purple", "red", "orange", "grey",
        "brown", "deep_orange", "dark_grey", "pink", "teal", "cyan",
        "lime", "light_green", "amber",
    ):
        assert color_id in src, f"missing swatch for {color_id!r}"


def test_click_handler_puts_ticket_id_and_color_id():
    src = SIDEBAR.read_text()
    idx = src.index("window.setTaskColor")
    block = src[idx : idx + 500]
    assert "method: 'PUT'" in block
    assert "ticket_id: TICKET_ID" in block
    assert "color_id: colorId" in block


def test_reverts_to_current_color_on_a_failed_save():
    """A failed/rejected save must not leave the wrong swatch highlighted
    — re-render against CURRENT_COLOR_ID, not the attempted colorId."""
    src = SIDEBAR.read_text()
    idx = src.index("window.setTaskColor")
    block = src[idx : idx + 700]
    assert "data.saved ? colorId : CURRENT_COLOR_ID" in block
