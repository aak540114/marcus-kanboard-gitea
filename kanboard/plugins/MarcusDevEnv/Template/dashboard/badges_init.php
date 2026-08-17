<?php
/**
 * Page-level batch fetch-and-fill for the per-project Marcus ON/OFF badges.
 *
 * Fired via the 'template:dashboard:show' hook, which app/Template/
 * dashboard/overview.php calls exactly once, unconditionally, at the very
 * end of the template (outside every isEmpty() branch) — so this runs
 * regardless of how many projects (zero or more) the row loop rendered.
 *
 * Each row's dashboard/project_badge.php already pushed its project id
 * into window.__marcusDashboardProjectIds and rendered a placeholder
 * <span id="marcus-dash-badge-{id}">. This script reads that shared
 * array and issues one fetch() per id against Marcus's existing
 * /api/project-enabled endpoint (already CORS-enabled and already used
 * the same way by board/header.php's agent-status badge), then updates
 * each placeholder in place.
 *
 * Relies on in-document-order execution of inline <script> tags: every
 * per-row script (which only pushes an id) runs before this one, since
 * overview.php emits all project rows before reaching the page-level
 * 'template:dashboard:show' hook at the end of the file.
 */
$marcusUrl = getenv('MARCUS_URL') ?: 'http://localhost:4298';
$marcusToken = getenv('MARCUS_AGENT_TOKEN') ?: '';
?>
<script>
(function () {
    var MARCUS_URL = <?= json_encode($marcusUrl) ?>;
    var MARCUS_TOKEN = <?= json_encode($marcusToken) ?>;
    var ids = window.__marcusDashboardProjectIds || [];

    function authHeaders() {
        var h = {};
        if (MARCUS_TOKEN) {
            h['Authorization'] = 'Bearer ' + MARCUS_TOKEN;
        }
        return h;
    }

    function renderBadge(el, enabled) {
        el.classList.remove(
            'marcus-dash-badge-checking',
            'marcus-dash-badge-on',
            'marcus-dash-badge-off',
            'marcus-dash-badge-error'
        );
        if (enabled === null) {
            el.classList.add('marcus-dash-badge-error');
            el.innerHTML = '&#9888; Marcus: unknown';
            el.title = 'Could not reach Marcus to check this project.';
            return;
        }
        if (enabled) {
            el.classList.add('marcus-dash-badge-on');
            el.innerHTML = '&#128275; Marcus: ON';
            el.title = "Marcus and AI agents may work this project's tickets.";
        } else {
            el.classList.add('marcus-dash-badge-off');
            el.innerHTML = '&#128274; Marcus: OFF';
            el.title = 'Marcus is not enabled for this project. Open its board to turn it on.';
        }
    }

    ids.forEach(function (pid) {
        var el = document.getElementById('marcus-dash-badge-' + pid);
        if (!el) {
            return;
        }
        fetch(MARCUS_URL + '/api/project-enabled?project_id=' + pid, {
            cache: 'no-store',
            headers: authHeaders()
        })
            .then(function (r) { return r.json(); })
            .then(function (data) { renderBadge(el, !!data.enabled); })
            .catch(function () { renderBadge(el, null); });
    });
}());
</script>
<style>
.marcus-dash-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 700;
    margin-left: 8px;
    border: 1px solid;
    white-space: nowrap;
    vertical-align: middle;
}
.marcus-dash-badge-checking { background: #f4f4f4; color: #888888; border-color: #dddddd; }
.marcus-dash-badge-on       { background: #f0fdf4; color: #15803d; border-color: #86efac; }
.marcus-dash-badge-off      { background: #fef2f2; color: #b91c1c; border-color: #fca5a5; }
.marcus-dash-badge-error    { background: #fff3e0; color: #b45309; border-color: #f8c97a; }
</style>
