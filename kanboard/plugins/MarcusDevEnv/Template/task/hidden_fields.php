<?php
/**
 * MarcusDevEnv task-summary field hider — injected at the very end of
 * Kanboard's own #task-summary section (hook 'template:task:details:bottom',
 * app/Template/task/details.php, right before its closing </section>).
 *
 * Marcus tickets don't use Kanboard's Swimlane/Priority/Position/Started
 * concepts (there's exactly one swimlane, priority and position are never
 * set by Marcus, and "Started" duplicates information already visible from
 * the column itself) — they're just clutter on the task page. Kanboard
 * gives plugins hook points to ADD content, not to edit an existing core
 * template, and app/Template/task/details.php renders those four fields as
 * plain <li><strong>Label:</strong> <span>...</span></li> rows with no
 * stable id/class to target and no hook of its own between them (several
 * sibling fields are also conditionally rendered, e.g. Reference/Complexity/
 * Category, so a fixed nth-child selector would be fragile across tickets).
 * So — same approach already used in task/sidebar.php's "Hide Kanboard's
 * native 'Start now' link" script — find each row by its <strong> label
 * text and hide the row. This never shadows the whole details.php template,
 * so future Kanboard upstream fixes to that file still apply on version
 * bumps.
 *
 * This hook fires textually AFTER all four target rows (it's the LAST
 * thing rendered inside <section id="task-summary">), so by the time this
 * inline <script> executes, every row it needs to inspect already exists
 * in the DOM — no DOMContentLoaded wait needed, and no flash-of-visible-
 * content window either.
 */
?>
<script>
(function () {
    var hiddenLabels = ['Swimlane:', 'Priority:', 'Position:', 'Started:'];
    document.querySelectorAll('#task-summary li').forEach(function (li) {
        var strong = li.querySelector('strong');
        if (strong && hiddenLabels.indexOf(strong.textContent.trim()) !== -1) {
            li.style.display = 'none';
        }
    });
}());
</script>
