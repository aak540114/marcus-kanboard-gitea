<?php
/**
 * MarcusDevEnv sidebar template — injected into the task detail sidebar.
 *
 * Section 1 — "View Live Changes" button
 *   Links to the Marcus /dev-env/view endpoint that spins up (or looks up
 *   an already-running) hot-reload dev environment for this ticket.
 *
 * Section 2 — "Dependencies" panel
 *   Shows which tickets this one depends on ("is blocked by") and which
 *   tickets depend on this one ("blocks"), fetched live from Marcus's
 *   /api/ticket-links endpoint.
 *
 * Variables available (set by Kanboard's template engine):
 *   $task  — associative array with at least 'id' and 'project_id'
 */

$marcusUrl = getenv('MARCUS_URL') ?: 'http://localhost:4298';
$ticketId  = $task['id'] ?? '';
$provider  = 'kanboard';

$viewUrl   = $marcusUrl
    . '/dev-env/view'
    . '?ticket_id=' . urlencode((string) $ticketId)
    . '&provider='  . urlencode($provider);

$linksUrl  = $marcusUrl
    . '/api/ticket-links'
    . '?ticket_id=' . urlencode((string) $ticketId);
?>

<!-- ── Section 1: Dev environment ─────────────────────────────────── -->
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

<!-- ── Section 2: Dependencies ────────────────────────────────────── -->
<style>
.marcus-deps { margin: 0; padding: 0; list-style: none; }
.marcus-deps li {
    padding: 3px 0;
    font-size: 12px;
    border-bottom: 1px solid rgba(0,0,0,.06);
    line-height: 1.4;
}
.marcus-deps li:last-child { border-bottom: none; }
.marcus-deps .dep-badge {
    display: inline-block;
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 10px;
    font-weight: 700;
    margin-right: 4px;
    vertical-align: middle;
}
.dep-badge-col { color: #666; font-size: 10px; }
.marcus-deps-empty { color: #aaa; font-size: 12px; font-style: italic; }
#marcus-deps-error { color: #b45309; font-size: 11px; }
</style>

<div class="sidebar-collapse">
    <h2 class="sidebar-title"><?= t('Marcus Dependencies') ?></h2>
    <div id="marcus-deps-loading" style="font-size:12px;color:#888;">Loading&hellip;</div>
    <div id="marcus-deps-content" style="display:none;">

        <p style="font-size:11px;font-weight:600;color:#555;margin:6px 0 2px;">
            <?= t('Depends on (must finish first):') ?>
        </p>
        <ul class="marcus-deps" id="marcus-deps-on"></ul>

        <p style="font-size:11px;font-weight:600;color:#555;margin:8px 0 2px;">
            <?= t('Blocks (waiting on this ticket):') ?>
        </p>
        <ul class="marcus-deps" id="marcus-deps-blocks"></ul>

        <p style="font-size:11px;font-weight:600;color:#555;margin:8px 0 2px;">
            <?= t('Related:') ?>
        </p>
        <ul class="marcus-deps" id="marcus-deps-relates"></ul>

    </div>
    <div id="marcus-deps-error" style="display:none;"></div>
</div>

<script>
(function () {
    var LINKS_URL  = <?= json_encode($linksUrl) ?>;
    var loadingEl  = document.getElementById('marcus-deps-loading');
    var contentEl  = document.getElementById('marcus-deps-content');
    var errorEl    = document.getElementById('marcus-deps-error');
    var onList     = document.getElementById('marcus-deps-on');
    var blocksList = document.getElementById('marcus-deps-blocks');
    var relList    = document.getElementById('marcus-deps-relates');

    // Colour palette for the status badges
    var BADGE_COLORS = {
        'ready':             { bg: '#dbeafe', fg: '#1e40af' },
        'in progress':       { bg: '#dcfce7', fg: '#166534' },
        'waiting for human': { bg: '#fef9c3', fg: '#854d0e' },
        'blocked':           { bg: '#fee2e2', fg: '#991b1b' },
        'done':              { bg: '#f3f4f6', fg: '#6b7280' },
    };

    function badgeStyle(column) {
        var key    = (column || '').toLowerCase();
        var colors = BADGE_COLORS[key] || { bg: '#f3f4f6', fg: '#374151' };
        return 'background:' + colors.bg + ';color:' + colors.fg + ';';
    }

    function renderList(ul, items, emptyMsg) {
        ul.innerHTML = '';
        if (!items || items.length === 0) {
            ul.innerHTML = '<li><span class="marcus-deps-empty">' + emptyMsg + '</span></li>';
            return;
        }
        items.forEach(function (item) {
            var li  = document.createElement('li');
            var col = item.column || '';
            li.innerHTML =
                '<span class="dep-badge" style="' + badgeStyle(col) + '">'
                + (col || '?')
                + '</span>'
                + '<strong>#' + item.task_id + '</strong>'
                + (item.title ? ' &mdash; ' + item.title : '');
            ul.appendChild(li);
        });
    }

    fetch(LINKS_URL, { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            loadingEl.style.display = 'none';
            contentEl.style.display = 'block';
            renderList(onList,     data.depends_on, 'No dependencies');
            renderList(blocksList,  data.blocks,    'Blocks nothing');
            renderList(relList,     data.relates_to, 'No related tickets');
        })
        .catch(function () {
            loadingEl.style.display = 'none';
            errorEl.style.display   = 'block';
            errorEl.textContent     = 'Could not reach Marcus at ' + LINKS_URL;
        });
}());
</script>
