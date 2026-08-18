"""
Guard: the Project Stats page's inline SVG bar chart (built by
``renderChart()`` inside ``src/marcus_mcp/server.py``'s
``project_stats_page``) draws each bar's hour label without it
overlapping the bar above it.

Regression: the label used to be anchored with text-anchor 'middle' and
then rotated +45deg around a point only 16px below the bars. For a
middle-anchored string, half its glyphs sit BEFORE the anchor point and
half AFTER; a positive rotation swings the "before" half up and to the
right of the anchor — i.e. back up into the bar sitting directly above
it, especially for longer labels (dates like "Aug 13, 2:00 PM"). The fix
anchors at 'end' and rotates -45deg instead: for an end-anchored string
every glyph precedes the anchor, and a negative rotation swings all of
them down-and-to-the-left, away from the bars, never back over them —
the same convention D3/Chart.js use for rotated axis labels.

There is no live-browser harness for this — this is a cheap static
regression guard on the embedded JS source text, same approach as the
sibling header.php tests.
"""

from pathlib import Path

SERVER = Path(__file__).resolve().parents[3] / "src/marcus_mcp/server.py"


def _render_chart_source() -> str:
    src = SERVER.read_text()
    start = src.index("function renderChart(")
    end = src.index("function refresh()", start)
    return src[start:end]


def test_hour_label_anchored_at_end_not_middle():
    block = _render_chart_source()
    hour_label_block = block[block.index("var tickX") :]
    assert "hourText.setAttribute('text-anchor', 'end')" in hour_label_block


def test_hour_label_rotates_negative_45_not_positive():
    block = _render_chart_source()
    hour_label_block = block[block.index("var tickX") :]
    assert "'rotate(-45 '" in hour_label_block
    # Regression guard: a bare "rotate(45 " (no leading '-') is the old,
    # broken direction — must not creep back in as the live transform.
    assert "'rotate(45 '" not in hour_label_block


def test_hour_label_tick_point_has_real_clearance_below_the_bars():
    """The old anchor sat only 16px below chartHeight — not enough
    clearance once you account for the label's own height when rotated.
    Guard against that shrinking back down."""
    block = _render_chart_source()
    assert "var tickY = chartHeight + 10;" in block
    assert "labelHeight = 70" in block


def test_hour_label_transform_rotates_about_its_own_tick_point():
    """The rotation center must be the label's own (tickX, tickY), not a
    stale/mismatched coordinate — otherwise the geometry above doesn't
    actually hold."""
    block = _render_chart_source()
    hour_label_block = block[block.index("var tickX") :]
    assert "'rotate(-45 ' + tickX + ' ' + tickY + ')'" in hour_label_block
