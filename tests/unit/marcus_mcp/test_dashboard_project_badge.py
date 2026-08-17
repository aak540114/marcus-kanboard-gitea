"""
Guard: the MarcusDevEnv plugin wires a Marcus ON/OFF badge into Kanboard's
own /dashboard page ("My projects" list), so a human can see at a glance
which projects Marcus is enabled for without opening each one.

Three files are involved:
  - kanboard/plugins/MarcusDevEnv/Plugin.php — registers the two hooks
    that make this possible (verified directly against Kanboard's
    v1.2.53 source, the exact tag pinned in docker-compose.yml; see the
    comment above the attach() calls for the citation).
  - .../Template/dashboard/project_badge.php — fired once per project
    row via 'template:dashboard:project:after-title'; renders a
    placeholder badge and registers the row's project id.
  - .../Template/dashboard/badges_init.php — fired once, page-level, via
    'template:dashboard:show'; batch-fetches /api/project-enabled for
    every registered id and fills in each placeholder.

There is no live Kanboard/browser harness in this environment — this is
a cheap static regression guard, same approach as
test_board_header_clone_project.py / test_board_header_project_stats.py.
"""

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "kanboard/plugins/MarcusDevEnv"
PLUGIN_PHP = PLUGIN_ROOT / "Plugin.php"
PROJECT_BADGE = PLUGIN_ROOT / "Template/dashboard/project_badge.php"
BADGES_INIT = PLUGIN_ROOT / "Template/dashboard/badges_init.php"


def test_dashboard_hooks_registered_with_exact_verified_names():
    src = PLUGIN_PHP.read_text()
    assert "'template:dashboard:project:after-title'" in src
    assert "'template:dashboard:show'" in src
    assert "'MarcusDevEnv:dashboard/project_badge'" in src
    assert "'MarcusDevEnv:dashboard/badges_init'" in src


def test_dashboard_template_files_exist():
    assert PROJECT_BADGE.is_file()
    assert BADGES_INIT.is_file()


def test_project_badge_uses_int_cast_on_project_id():
    src = PROJECT_BADGE.read_text()
    # $project['id'] is DB-sourced, but this is still the established
    # XSS-safety convention in this plugin (matches header.php's
    # (int) $projectId) — an int can never break out of an HTML
    # attribute or an inline <script> value.
    assert "(int) ($project['id']" in src


def test_project_badge_guards_against_missing_or_zero_id():
    src = PROJECT_BADGE.read_text()
    assert "if ($projectId <= 0)" in src
    assert "return;" in src


def test_project_badge_placeholder_and_shared_array_wired():
    src = PROJECT_BADGE.read_text()
    assert 'id="marcus-dash-badge-<?= $projectId ?>"' in src
    assert "window.__marcusDashboardProjectIds" in src
    assert "window.__marcusDashboardProjectIds.push(<?= $projectId ?>)" in src


def test_badges_init_reads_shared_array_and_hits_project_enabled_api():
    src = BADGES_INIT.read_text()
    assert "window.__marcusDashboardProjectIds" in src
    assert "/api/project-enabled?project_id=" in src


def test_badges_init_sends_bearer_auth_when_token_configured():
    src = BADGES_INIT.read_text()
    assert "MARCUS_AGENT_TOKEN" in src
    assert "'Authorization'" in src
    assert "'Bearer '" in src


def test_badges_init_updates_dom_element_by_row_id():
    src = BADGES_INIT.read_text()
    assert "document.getElementById('marcus-dash-badge-' + pid)" in src


def test_badges_init_handles_fetch_failure_distinctly_from_off():
    """A network/Marcus-down failure must not be rendered as 'OFF' —
    those are different facts (unknown vs. confirmed-disabled)."""
    src = BADGES_INIT.read_text()
    assert ".catch(function () { renderBadge(el, null); });" in src
    assert "marcus-dash-badge-error" in src
    assert "marcus-dash-badge-off" in src


def test_badges_init_php_values_json_encoded_not_raw_interpolated():
    src = BADGES_INIT.read_text()
    # The vulnerable form would splice getenv() output directly into a
    # JS string literal (breakable via an embedded quote); this project
    # always goes through json_encode() for PHP->JS handoff of
    # env-sourced strings.
    assert "json_encode($marcusUrl)" in src
    assert "json_encode($marcusToken)" in src
