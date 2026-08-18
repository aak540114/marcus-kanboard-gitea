"""
Guard: the MarcusDevEnv plugin hides Swimlane/Priority/Position/Started
from Kanboard's task-detail page, and strips the visible
"<!-- MARCUS_AC_START -->" / "<!-- MARCUS_AC_END -->" sentinel text left
behind by Kanboard's Markdown renderer in the task description.

Both are display-only DOM fixups injected via Kanboard's plugin hook
system (Kanboard gives plugins ADD-only hook points into its own core
templates, not template-editing ones — see the precedent already
established by task/sidebar.php's "Hide Kanboard's native 'Start now'
link" script). Neither script may touch what's actually stored in a
ticket's description: src/core/acceptance_criteria.py's ACParser/
ACChangeDetector re-parse the AC sentinel markers on every read, and
stripping them from storage would silently break human-edit detection.

There is no live-browser harness for this plugin — this is a cheap
static regression guard, same approach as the sibling header.php tests.
"""

from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "kanboard/plugins/MarcusDevEnv"
PLUGIN_PHP = PLUGIN_DIR / "Plugin.php"
HIDDEN_FIELDS = PLUGIN_DIR / "Template/task/hidden_fields.php"
DESCRIPTION_CLEANUP = PLUGIN_DIR / "Template/task/description_cleanup.php"


def test_hidden_fields_template_exists():
    assert HIDDEN_FIELDS.is_file()


def test_hidden_fields_hides_exactly_the_four_requested_labels():
    src = HIDDEN_FIELDS.read_text()
    assert "['Swimlane:', 'Priority:', 'Position:', 'Started:']" in src


def test_hidden_fields_matches_by_task_summary_li_strong_text():
    """Must scope to #task-summary li and read the <strong> label — a
    looser selector could accidentally hide unrelated page content that
    happens to contain the same words."""
    src = HIDDEN_FIELDS.read_text()
    assert "document.querySelectorAll('#task-summary li')" in src
    assert "li.querySelector('strong')" in src
    assert "strong.textContent.trim()" in src


def test_hidden_fields_does_not_touch_status_column_or_other_fields():
    """Regression guard: fields the user did NOT ask to hide (Status,
    Column, Assignee, Created, Modified, Due date, etc.) must stay
    visible — the hidden list must be exactly the four requested labels,
    nothing broader."""
    src = HIDDEN_FIELDS.read_text()
    for untouched_label in (
        "'Status:'",
        "'Column:'",
        "'Assignee:'",
        "'Created:'",
        "'Modified:'",
        "'Due date:'",
    ):
        assert untouched_label not in src


def test_description_cleanup_template_exists():
    assert DESCRIPTION_CLEANUP.is_file()


def test_description_cleanup_strips_exact_ac_sentinel_strings():
    src = DESCRIPTION_CLEANUP.read_text()
    assert "'<!-- MARCUS_AC_START -->'" in src
    assert "'<!-- MARCUS_AC_END -->'" in src


def test_description_cleanup_scopes_to_markdown_article_only():
    """Must only touch the rendered .markdown article (the description),
    not the whole page — a comment or attachment filename that happens to
    contain the marker text elsewhere on the page must not be touched."""
    src = DESCRIPTION_CLEANUP.read_text()
    assert "document.querySelectorAll('.markdown')" in src


def test_description_cleanup_uses_text_node_replacement_not_innerhtml():
    """Must mutate textContent on individual text nodes (found via
    createTreeWalker), never innerHTML — an innerHTML rewrite of
    server-rendered content is exactly the stored-XSS pattern already
    fixed elsewhere in this plugin (see test_board_header_escaping.py /
    test_board_header_clone_project.py)."""
    src = DESCRIPTION_CLEANUP.read_text()
    assert "createTreeWalker" in src
    assert "node.textContent = stripped" in src
    assert ".innerHTML" not in src


def test_plugin_php_registers_both_new_hooks():
    src = PLUGIN_PHP.read_text()
    assert "'template:task:details:bottom'" in src
    assert "'MarcusDevEnv:task/hidden_fields'" in src
    assert "'template:task:show:before-subtasks'" in src
    assert "'MarcusDevEnv:task/description_cleanup'" in src


def test_plugin_php_hidden_fields_registered_on_details_bottom_not_top():
    """template:task:details:bottom is rendered as the LAST thing inside
    <section id="task-summary"> (verified directly against Kanboard's
    real app/Template/task/details.php on the v1.2.53 release tag) — the
    hidden_fields template must be wired to that hook specifically, not
    an earlier one like template:task:details:top, where the target rows
    wouldn't exist in the DOM yet when the script runs."""
    src = PLUGIN_PHP.read_text()
    idx = src.index("MarcusDevEnv:task/hidden_fields")
    preceding_attach_call = src[:idx].rsplit("$this->template->hook->attach(", 1)[-1]
    assert "template:task:details:bottom" in preceding_attach_call


def test_plugin_php_description_cleanup_registered_on_before_subtasks():
    """template:task:show:before-subtasks fires immediately AFTER
    task/description.php renders (verified against Kanboard's real
    app/Template/task/show.php) — the description_cleanup template must
    be wired to that hook specifically, not an earlier one like
    template:task:show:top or template:task:show:before-description,
    where the .markdown article wouldn't exist in the DOM yet."""
    src = PLUGIN_PHP.read_text()
    idx = src.index("MarcusDevEnv:task/description_cleanup")
    preceding_attach_call = src[:idx].rsplit("$this->template->hook->attach(", 1)[-1]
    assert "template:task:show:before-subtasks" in preceding_attach_call
