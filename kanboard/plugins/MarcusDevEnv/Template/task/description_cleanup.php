<?php
/**
 * MarcusDevEnv LEGACY AC-sentinel-marker hider — injected right after
 * Kanboard's task description block (hook
 * 'template:task:show:before-subtasks', app/Template/task/show.php, fired
 * immediately after task/description.php renders and before
 * subtask/show.php).
 *
 * src/core/acceptance_criteria.py now embeds the acceptance-criteria
 * block using Markdown reference-link sentinels ("[MARCUS_AC_START]: #
 * (marker)" / "[MARCUS_AC_END]: # (marker)"), which every Markdown
 * renderer — Kanboard included — consumes silently as link-reference
 * metadata and never renders as visible text. Older tickets, embedded
 * before that switch, still have the ORIGINAL sentinels: two HTML
 * comments, "<!-- MARCUS_AC_START -->" and "<!-- MARCUS_AC_END -->" (see
 * ACParser's module docstring for the full history). ACParser.extract()
 * still reads both formats — this script exists only to hide the old
 * one's fallout in the browser until a ticket's AC is next regenerated
 * or re-embedded, at which point it upgrades to the new sentinels and
 * this cleanup stops being needed for it.
 *
 * Why the old format needed hiding at all: Kanboard renders the
 * description through a Markdown parser with setMarkupEscaped(true)
 * (app/Helper/TextHelper.php::markdown(), backed by MARKDOWN_ESCAPE_HTML
 * which defaults to true — app/constants.php). That escapes ALL raw HTML
 * found in the text, comments included, instead of treating
 * "<!-- ... -->" as an invisible HTML comment: the two sentinel lines
 * ended up as ordinary, VISIBLE text on the task page (each one gets its
 * own paragraph, since neither line matches any Markdown block syntax
 * and an ATX heading / list is free to interrupt a paragraph on the very
 * next line). That's the literal text a human sees and finds confusing —
 * this script removes exactly that, and only in the browser's rendered
 * DOM. It never touches the description Marcus reads/writes through the
 * API, so ACParser/ACChangeDetector keep working unchanged.
 *
 * Same "hide by matching, don't shadow the template" approach as
 * task/sidebar.php's "Start now" link hider and this plugin's own
 * hidden_fields.php. Runs right after the description's
 * <article class="markdown"> has been parsed into the DOM (this hook
 * fires strictly after it, before subtasks) — no DOMContentLoaded wait
 * needed.
 */
?>
<script>
(function () {
    var markers = ['<!-- MARCUS_AC_START -->', '<!-- MARCUS_AC_END -->'];
    document.querySelectorAll('.markdown').forEach(function (article) {
        var walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT, null, false);
        var node;
        var touchedParents = [];
        while ((node = walker.nextNode())) {
            var original = node.textContent;
            var stripped = original;
            markers.forEach(function (marker) {
                stripped = stripped.split(marker).join('');
            });
            if (stripped !== original) {
                node.textContent = stripped;
                touchedParents.push(node.parentElement);
            }
        }
        // A marker line that had its own paragraph is now an empty
        // paragraph — collapse it so it doesn't leave a blank line behind.
        touchedParents.forEach(function (el) {
            if (el && el.textContent.trim() === '' && !el.querySelector('img, a, br')) {
                el.style.display = 'none';
            }
        });
    });
}());
</script>
