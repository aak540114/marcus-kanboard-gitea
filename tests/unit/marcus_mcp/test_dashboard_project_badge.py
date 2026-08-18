"""
Guard: the MarcusDevEnv plugin wires a Marcus ON/OFF badge into Kanboard's
"My projects" list, so a human can see at a glance which projects Marcus is
enabled for without opening each one.

Two files are involved:
  - kanboard/plugins/MarcusDevEnv/Plugin.php — registers the single hook
    that makes this possible (verified directly against Kanboard's
    v1.2.53 source, the exact tag pinned in docker-compose.yml; see the
    comment above the attach() call for the citation).
  - .../Template/dashboard/project_badge.php — fired once per project row
    via 'template:dashboard:project:after-title'; fully self-contained
    (renders a placeholder, then fetches and fills in its OWN status).

Regression this guards against: an earlier version split the work across
TWO files — project_badge.php (placeholder only) and a page-level
batch-fetch script fired via 'template:dashboard:show'. That hook fires
only from app/Template/dashboard/overview.php (the bare /dashboard URL)
— app/Template/dashboard/projects.php (the sidebar's "My projects" link
at /dashboard/projects) has no equivalent, even though its
project_list/project_title.php partial DOES fire the same
'template:dashboard:project:after-title' hook this badge is registered
on. So on /dashboard/projects, the placeholder rendered but the
fetch-and-fill script never loaded — every row was stuck on "checking…"
forever. Making the badge fully self-contained (this file) fixes both
pages uniformly.

There is no live Kanboard/browser harness in this environment — this is
a cheap static regression guard, same approach as
test_board_header_clone_project.py / test_board_header_project_stats.py.
"""

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "kanboard/plugins/MarcusDevEnv"
PLUGIN_PHP = PLUGIN_ROOT / "Plugin.php"
PROJECT_BADGE = PLUGIN_ROOT / "Template/dashboard/project_badge.php"
BADGES_INIT = PLUGIN_ROOT / "Template/dashboard/badges_init.php"


def test_dashboard_hook_registered_with_exact_verified_name():
    src = PLUGIN_PHP.read_text()
    assert "'template:dashboard:project:after-title'" in src
    assert "'MarcusDevEnv:dashboard/project_badge'" in src


def test_badges_init_no_longer_exists_or_registered():
    """Regression guard: the old page-level batch-fetch file (and its
    'template:dashboard:show' hook, which never fires on
    /dashboard/projects) must not creep back in as an ACTUAL
    registration — that split-file design is exactly what broke the
    badge on that page. (The string may still legitimately appear in
    this file's own explanatory comment about that history — only a
    live attach() call matters here.)"""
    assert not BADGES_INIT.exists()
    src = PLUGIN_PHP.read_text()
    assert "dashboard/badges_init" not in src
    assert (
        "attach(\n            'template:dashboard:show'" not in src
    )


def test_project_badge_template_exists():
    assert PROJECT_BADGE.is_file()


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


def test_project_badge_renders_placeholder_and_fetches_its_own_status():
    src = PROJECT_BADGE.read_text()
    assert 'id="marcus-dash-badge-<?= $projectId ?>"' in src
    assert (
        "document.getElementById('marcus-dash-badge-<?= $projectId ?>')" in src
    )
    assert "/api/project-enabled?project_id=<?= $projectId ?>" in src


def test_project_badge_no_longer_depends_on_a_shared_page_level_array():
    """Regression guard: must not reintroduce the
    window.__marcusDashboardProjectIds hand-off — that's exactly the
    cross-file dependency that broke on /dashboard/projects."""
    src = PROJECT_BADGE.read_text()
    assert "__marcusDashboardProjectIds" not in src


def test_project_badge_sends_bearer_auth_when_token_configured():
    src = PROJECT_BADGE.read_text()
    assert "MARCUS_AGENT_TOKEN" in src
    assert "'Authorization'" in src
    assert "'Bearer '" in src


def test_project_badge_handles_fetch_failure_distinctly_from_off():
    """A network/Marcus-down failure must not be rendered as 'OFF' —
    those are different facts (unknown vs. confirmed-disabled)."""
    src = PROJECT_BADGE.read_text()
    assert ".catch(function () { renderBadge(null); });" in src
    assert "marcus-dash-badge-error" in src
    assert "marcus-dash-badge-off" in src


def test_project_badge_php_values_json_encoded_not_raw_interpolated():
    src = PROJECT_BADGE.read_text()
    # The vulnerable form would splice getenv() output directly into a
    # JS string literal (breakable via an embedded quote); this project
    # always goes through json_encode() for PHP->JS handoff of
    # env-sourced strings.
    assert "json_encode($marcusUrl)" in src
    assert "json_encode($marcusToken)" in src


def test_project_badge_includes_its_own_styles():
    """Since there is no longer a shared page-level script to own the
    CSS, each row must carry it — harmless duplication for a typical
    project count, and the only way to guarantee it loads on both
    /dashboard and /dashboard/projects."""
    src = PROJECT_BADGE.read_text()
    assert ".marcus-dash-badge-on" in src
    assert ".marcus-dash-badge-off" in src
    assert ".marcus-dash-badge-error" in src
