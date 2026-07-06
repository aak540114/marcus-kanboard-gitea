<?php
/**
 * MarcusDevEnv sidebar template — injected into the task detail sidebar.
 *
 * Renders a "View Live Changes" button that links to the Marcus
 * /dev-env/view endpoint.  Marcus starts the dev environment on demand
 * and redirects the browser to the hot-reload port.
 *
 * Variables available (set by Kanboard's template engine):
 *   $task  — associative array with at least 'id' and 'project_id'
 */

// Allow overriding via the MARCUS_URL environment variable; fall back to
// the standard localhost default used in the marcus-kanboard-gitlab stack.
$marcusUrl = getenv('MARCUS_URL') ?: 'http://localhost:4298';
$ticketId  = $task['id'] ?? '';
$provider  = 'kanboard';

$viewUrl = $marcusUrl
    . '/dev-env/view'
    . '?ticket_id=' . urlencode((string) $ticketId)
    . '&provider=' . urlencode($provider);
?>
<div class="sidebar-collapse">
    <h2 class="sidebar-title"><?= t('Marcus Dev Environment') ?></h2>
    <ul>
        <li>
            <a href="<?= htmlspecialchars($viewUrl, ENT_QUOTES, 'UTF-8') ?>"
               target="_blank"
               rel="noopener noreferrer"
               class="btn btn-info btn-block"
               style="display:block;text-align:center;padding:6px 12px;margin-top:4px;">
                &#128064; <?= t('View Live Changes') ?>
            </a>
        </li>
    </ul>
</div>
