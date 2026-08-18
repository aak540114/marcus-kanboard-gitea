<?php
/**
 * Per-project-row Marcus ON/OFF badge — fired via the
 * 'template:dashboard:project:after-title' hook.
 *
 * Fully self-contained: renders a placeholder, then immediately fetches
 * and fills in ITS OWN status. Kanboard fires this SAME hook, with the
 * SAME $project array, from both app/Template/dashboard/overview.php
 * (DashboardController::show(), the bare /dashboard URL) AND
 * app/Template/project_list/project_title.php (rendered by
 * app/Template/dashboard/projects.php, the sidebar's "My projects" link
 * at /dashboard/projects) — verified directly against both files on the
 * v1.2.53 release tag actually shipped by the kanboard/kanboard:latest
 * Docker image.
 *
 * A previous version of this badge only rendered a placeholder here and
 * relied on a SEPARATE page-level script (dashboard/badges_init.php, on
 * the 'template:dashboard:show' hook) to batch-fetch every row's status
 * together. That hook fires only from overview.php — projects.php never
 * calls it — so on /dashboard/projects the fetch-and-fill script and its
 * CSS never loaded at all, and every row was stuck on the raw "checking…"
 * placeholder forever. Making each row self-sufficient fixes both pages
 * uniformly and removes the cross-file/cross-hook-timing dependency
 * entirely — see dashboard/badges_init.php's history for the bug this
 * replaced (that file and its 'template:dashboard:show' registration in
 * Plugin.php have been removed).
 *
 * $project is supplied by the hook call site — the same array driving
 * that row's own title/lock-icon rendering.
 */
$projectId = (int) ($project['id'] ?? 0);
if ($projectId <= 0) {
    return;
}
$marcusUrl = getenv('MARCUS_URL') ?: 'http://localhost:4298';
$marcusToken = getenv('MARCUS_AGENT_TOKEN') ?: '';
?>
<span class="marcus-dash-badge marcus-dash-badge-checking"
      id="marcus-dash-badge-<?= $projectId ?>"
      title="Checking whether Marcus is enabled for this project&hellip;">
    &#8987; Marcus
</span>
<script>
(function () {
    var MARCUS_URL = <?= json_encode($marcusUrl) ?>;
    var MARCUS_TOKEN = <?= json_encode($marcusToken) ?>;
    var el = document.getElementById('marcus-dash-badge-<?= $projectId ?>');
    if (!el) {
        return;
    }

    function authHeaders() {
        var h = {};
        if (MARCUS_TOKEN) {
            h['Authorization'] = 'Bearer ' + MARCUS_TOKEN;
        }
        return h;
    }

    function renderBadge(enabled) {
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

    fetch(MARCUS_URL + '/api/project-enabled?project_id=<?= $projectId ?>', {
        cache: 'no-store',
        headers: authHeaders()
    })
        .then(function (r) { return r.json(); })
        .then(function (data) { renderBadge(!!data.enabled); })
        .catch(function () { renderBadge(null); });
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
