<?php
/**
 * Per-project-row Marcus ON/OFF badge — fired via the
 * 'template:dashboard:project:after-title' hook, once per project row in
 * app/Template/dashboard/overview.php's "My projects" list (the template
 * DashboardController::show() renders for the bare /dashboard URL).
 *
 * Only renders a placeholder + registers the project id here; the actual
 * fetch-and-fill happens once for every row together, from the
 * page-level script emitted by dashboard/badges_init.php (see Plugin.php
 * for why: one shared batch fetch beats one inline <script> per row).
 *
 * $project is supplied by the hook call site (overview.php) — the same
 * array driving that row's own title/lock-icon rendering.
 */
$projectId = (int) ($project['id'] ?? 0);
if ($projectId <= 0) {
    return;
}
?>
<span class="marcus-dash-badge marcus-dash-badge-checking"
      id="marcus-dash-badge-<?= $projectId ?>"
      data-marcus-project-id="<?= $projectId ?>"
      title="Checking whether Marcus is enabled for this project&hellip;">
    &#8987; Marcus
</span>
<script>
(function () {
    window.__marcusDashboardProjectIds = window.__marcusDashboardProjectIds || [];
    window.__marcusDashboardProjectIds.push(<?= $projectId ?>);
}());
</script>
