<?php
/**
 * MarcusDevEnv sidebar template — injected into the task detail sidebar.
 *
 * Section 1 — Dev Environment panel
 *   Polls Marcus /api/dev-env/status on load to decide whether to show a
 *   "Start Preview" or "Open / Stop Preview" UI.  The Stop button calls
 *   /dev-env/stop and immediately tears down the Docker container without
 *   waiting for the ticket to be closed.
 *
 * Section 2 — Gate Mode panel
 *   Per-ticket Human Gate / AI Gate toggle.  Shows the project-level default
 *   and lets the human override it for this ticket only.
 *
 * Section 3 — "Dependencies" panel
 *   Shows which tickets this one depends on ("is blocked by") and which
 *   tickets depend on this one ("blocks"), fetched live from Marcus's
 *   /api/ticket-links endpoint.
 *
 * Variables available (set by Kanboard's template engine):
 *   $task  — associative array with at least 'id' and 'project_id'
 */

$marcusUrl  = getenv('MARCUS_URL') ?: 'http://localhost:4298';
$ticketId   = $task['id'] ?? '';
$provider   = 'kanboard';
$projectId  = $task['project_id'] ?? '';

$viewUrl = $marcusUrl
    . '/dev-env/view'
    . '?ticket_id='  . urlencode((string) $ticketId)
    . '&provider='   . urlencode($provider)
    . '&project_id=' . urlencode((string) $projectId);

$stopUrl = $marcusUrl
    . '/dev-env/stop'
    . '?ticket_id=' . urlencode((string) $ticketId)
    . '&provider='  . urlencode($provider);

$statusUrl = $marcusUrl
    . '/api/dev-env/status'
    . '?ticket_id=' . urlencode((string) $ticketId)
    . '&provider='  . urlencode($provider);

$linksUrl = $marcusUrl
    . '/api/ticket-links'
    . '?ticket_id=' . urlencode((string) $ticketId);

$gateApiBase = $marcusUrl . '/api/gate-setting';
?>

<!-- ── Section 1: Dev environment ─────────────────────────────────── -->
<style>
#marcus-dev-env-panel .btn { display:block; text-align:center; padding:6px 12px; margin-top:4px; }
#marcus-dev-env-status-msg { font-size:11px; color:#888; margin-top:4px; }

/* ── Gate toggle (sidebar) ──────────────────────────────────────── */
.m-gate-section { margin-top: 4px; }
.m-gate-desc {
    font-size: 11px;
    color: #888;
    margin-bottom: 6px;
    line-height: 1.4;
}
.m-gate-row {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 4px;
}
.m-gate-row-label {
    font-size: 10px;
    font-weight: 700;
    color: #555;
    text-transform: uppercase;
    letter-spacing: .04em;
    min-width: 52px;
}
.m-gate-pills {
    display: inline-flex;
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid #d1d5db;
    background: #f9fafb;
}
.m-gate-pills button {
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 600;
    border: none;
    cursor: pointer;
    background: transparent;
    color: #6b7280;
    transition: background 0.12s, color 0.12s;
    white-space: nowrap;
}
.m-gate-pills button.on-human { background:#dbeafe; color:#1d4ed8; }
.m-gate-pills button.on-ai    { background:#f3e8ff; color:#7c3aed; }
.m-gate-pills button.on-inherit { background:#ecfdf5; color:#065f46; }
.m-gate-pills button:disabled { opacity:.5; cursor:default; }
.m-gate-eff {
    font-size: 11px;
    color: #6b7280;
    padding: 2px 0 0;
}
.m-gate-saving { font-size:10px; color:#9ca3af; display:none; }

/* ── AI Verify (sidebar) ─────────────────────────────────────────── */
.m-verify-section {
    margin-top: 8px;
    padding-top: 6px;
    border-top: 1px solid rgba(0,0,0,.08);
    display: none; /* shown only when effective gate is AI */
}
.m-verify-section.visible { display: block; }
.m-verify-row {
    display: flex;
    align-items: center;
    gap: 6px;
}
.m-verify-desc {
    font-size: 10px;
    color: #888;
    margin: 3px 0 6px;
    line-height: 1.4;
}
.m-verify-switch {
    position: relative;
    display: inline-block;
    width: 32px;
    height: 18px;
    flex-shrink: 0;
}
.m-verify-switch input { opacity: 0; width: 0; height: 0; }
.m-verify-slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background: #d1d5db;
    border-radius: 18px;
    transition: background 0.2s;
}
.m-verify-slider:before {
    content: '';
    position: absolute;
    width: 12px; height: 12px;
    left: 3px; bottom: 3px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s;
}
.m-verify-switch input:checked + .m-verify-slider { background: #7c3aed; }
.m-verify-switch input:checked + .m-verify-slider:before { transform: translateX(14px); }
.m-verify-badge {
    font-size: 10px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 4px;
    background: #f3e8ff;
    color: #7c3aed;
}
.m-verify-badge.off { background: #f3f4f6; color: #6b7280; }
.m-verify-inherit-row {
    margin-top: 4px;
    font-size: 10px;
    color: #9ca3af;
}
</style>

<div class="sidebar-collapse">
    <h2 class="sidebar-title"><?= t('Marcus Dev Environment') ?></h2>
    <ul>
        <li id="marcus-dev-env-panel">
            <span style="font-size:12px;color:#aaa;">Checking status&hellip;</span>
        </li>
    </ul>
    <p id="marcus-dev-env-status-msg"></p>
</div>

<!-- ── Section 2: Gate mode ───────────────────────────────────────── -->
<div class="sidebar-collapse">
    <h2 class="sidebar-title"><?= t('Marcus Gate Mode') ?></h2>
    <div class="m-gate-section">
        <p class="m-gate-desc">
            <strong>Human Gate</strong>: AI pauses for human review before marking done.<br>
            <strong>AI Gate</strong>: AI works autonomously from ready to done.
        </p>

        <!-- Project-level row (read-only indicator) -->
        <div class="m-gate-row">
            <span class="m-gate-row-label">Project</span>
            <div class="m-gate-pills" id="marcus-pg-pills">
                <button id="pgBtn-human" disabled>&#128100; Human</button>
                <button id="pgBtn-ai"    disabled>&#129302; AI</button>
            </div>
        </div>

        <!-- Per-ticket row (editable) -->
        <div class="m-gate-row">
            <span class="m-gate-row-label">This ticket</span>
            <div class="m-gate-pills" id="marcus-tg-pills">
                <button id="tgBtn-inherit" onclick="setTicketGate(null)">&#10226; Inherit</button>
                <button id="tgBtn-human"   onclick="setTicketGate('human')">&#128100; Human</button>
                <button id="tgBtn-ai"      onclick="setTicketGate('ai')">&#129302; AI</button>
            </div>
            <span class="m-gate-saving" id="marcus-tg-saving">saving&hellip;</span>
        </div>

        <!-- Resolved effective gate -->
        <div class="m-gate-eff">
            Effective: <strong id="marcus-eff-gate">loading&hellip;</strong>
        </div>

        <!-- AI Verify (only shown when effective gate is AI) -->
        <div class="m-verify-section" id="marcus-verify-section">
            <p class="m-verify-desc">
                When <strong>AI Verify</strong> is on, a second AI agent reviews
                the implementation before merging. Issues are posted as a comment
                and the worker agent must fix them.
            </p>
            <!-- Per-ticket verify override -->
            <div class="m-verify-row">
                <label class="m-verify-switch" title="Toggle AI verification for this ticket">
                    <input type="checkbox" id="marcus-verify-chk" onchange="setTicketVerify(this.checked)">
                    <span class="m-verify-slider"></span>
                </label>
                <span id="marcus-verify-badge" class="m-verify-badge off">Off</span>
                <span class="m-gate-saving" id="marcus-verify-saving">saving&hellip;</span>
            </div>
            <div class="m-verify-inherit-row" id="marcus-verify-inherit-note"></div>
        </div>
    </div>
</div>

<!-- ── Section 3: Dependencies ────────────────────────────────────── -->
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
    /* ── URLs injected from PHP ──────────────────────────────────── */
    var VIEW_URL     = <?= json_encode($viewUrl) ?>;
    var STOP_URL     = <?= json_encode($stopUrl) ?>;
    var STATUS_URL   = <?= json_encode($statusUrl) ?>;
    var LINKS_URL    = <?= json_encode($linksUrl) ?>;
    var GATE_URL     = <?= json_encode($gateApiBase) ?>;
    var TICKET_ID    = <?= json_encode((string) $ticketId) ?>;
    var PROJECT_ID   = <?= json_encode((int) $projectId) ?>;

    /* ── Dev-environment panel ───────────────────────────────────── */
    var devPanel  = document.getElementById('marcus-dev-env-panel');
    var statusMsg = document.getElementById('marcus-dev-env-status-msg');

    function setMsg(text) { statusMsg.textContent = text; }

    function renderStopped() {
        devPanel.innerHTML =
            '<a href="' + VIEW_URL + '" target="_blank" rel="noopener noreferrer" '
            + 'class="btn btn-info btn-block">'
            + '&#128064; Start Preview'
            + '</a>';
        setMsg('No preview running.');
    }

    function renderRunning(previewUrl) {
        devPanel.innerHTML =
            '<a href="' + previewUrl + '" target="_blank" rel="noopener noreferrer" '
            + 'class="btn btn-success btn-block">'
            + '&#127758; Open Preview'
            + '</a>'
            + '<button class="btn btn-danger btn-block" id="marcus-stop-btn">'
            + '&#9632; Stop Preview'
            + '</button>';
        setMsg('Preview running at ' + previewUrl);

        document.getElementById('marcus-stop-btn').addEventListener('click', function () {
            this.disabled = true;
            this.textContent = 'Stopping…';
            fetch(STOP_URL, { method: 'POST', cache: 'no-store' })
                .then(function (r) { return r.json(); })
                .then(function () { renderStopped(); setMsg('Preview stopped.'); })
                .catch(function () {
                    setMsg('Could not reach Marcus to stop the preview.');
                    renderStopped();
                });
        });
    }

    fetch(STATUS_URL, { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.running && data.url) { renderRunning(data.url); }
            else { renderStopped(); }
        })
        .catch(function () {
            renderStopped();
            setMsg('Marcus is unreachable. Start anyway to launch a new preview.');
        });

    /* ── Gate mode panel ─────────────────────────────────────────── */
    var saving       = document.getElementById('marcus-tg-saving');
    var effEl        = document.getElementById('marcus-eff-gate');
    var verifySection = document.getElementById('marcus-verify-section');
    var verifyChk    = document.getElementById('marcus-verify-chk');
    var verifyBadge  = document.getElementById('marcus-verify-badge');
    var verifySaving = document.getElementById('marcus-verify-saving');
    var verifyNote   = document.getElementById('marcus-verify-inherit-note');

    // Apply visual state to project-level pill row (read-only indicator)
    function applyProjectPills(gate) {
        var h = document.getElementById('pgBtn-human');
        var a = document.getElementById('pgBtn-ai');
        h.className = gate === 'human' ? 'on-human' : '';
        a.className = gate === 'ai'    ? 'on-ai'    : '';
    }

    // Apply visual state to ticket-level pill row
    function applyTicketPills(ticketGate) {
        var iBtn = document.getElementById('tgBtn-inherit');
        var hBtn = document.getElementById('tgBtn-human');
        var aBtn = document.getElementById('tgBtn-ai');
        iBtn.className = ticketGate === null      ? 'on-inherit' : '';
        hBtn.className = ticketGate === 'human'   ? 'on-human'   : '';
        aBtn.className = ticketGate === 'ai'      ? 'on-ai'      : '';
    }

    function applyEffective(effective) {
        var labels = { human: '👤 Human Gate', ai: '🤖 AI Gate' };
        effEl.textContent = labels[effective] || effective;
        effEl.style.color = effective === 'ai' ? '#7c3aed' : '#1d4ed8';
        // Show AI Verify section only when effective gate is AI
        if (effective === 'ai') {
            verifySection.classList.add('visible');
        } else {
            verifySection.classList.remove('visible');
        }
    }

    function applyVerify(ticketVerify, projectVerify, effectiveVerify) {
        verifyChk.checked = !!effectiveVerify;
        verifyBadge.textContent = effectiveVerify ? 'On' : 'Off';
        verifyBadge.className = 'marcus-verify-badge' + (effectiveVerify ? '' : ' off');
        // Show inherit note when ticket has no explicit setting
        if (ticketVerify === null || ticketVerify === undefined) {
            var src = projectVerify ? 'project: On' : 'project: Off';
            verifyNote.textContent = '(inheriting from ' + src + ')';
        } else {
            verifyNote.textContent = '';
        }
    }

    function loadGateSettings() {
        var url = GATE_URL + '?project_id=' + PROJECT_ID + '&ticket_id=' + encodeURIComponent(TICKET_ID);
        fetch(url, { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                applyProjectPills(data.project_gate || 'human');
                applyTicketPills(data.ticket_gate);          // null = inheriting
                applyEffective(data.effective || 'human');
                applyVerify(data.ticket_verify, data.project_verify, data.effective_verify);
            })
            .catch(function () {
                applyProjectPills('human');
                applyTicketPills(null);
                applyEffective('human');
                applyVerify(null, false, false);
            });
    }

    loadGateSettings();

    // Called by button onclick handlers
    window.setTicketGate = function (gate) {
        saving.style.display = 'inline';
        var btns = document.querySelectorAll('#marcus-tg-pills button');
        btns.forEach(function (b) { b.disabled = true; });

        fetch(GATE_URL + '/ticket', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ticket_id: TICKET_ID, gate: gate }),
        })
        .then(function (r) { return r.json(); })
        .then(function () { loadGateSettings(); })
        .catch(function () { /* visual state stays, user can retry */ })
        .finally(function () {
            btns.forEach(function (b) { b.disabled = false; });
            saving.style.display = 'none';
        });
    };

    // Called by AI Verify toggle
    window.setTicketVerify = function (enabled) {
        verifySaving.style.display = 'inline';
        verifyChk.disabled = true;

        fetch(GATE_URL + '/ticket', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ticket_id: TICKET_ID, verify: enabled }),
        })
        .then(function (r) { return r.json(); })
        .then(function () { loadGateSettings(); })
        .catch(function () { /* keep current visual state */ })
        .finally(function () {
            verifyChk.disabled = false;
            verifySaving.style.display = 'none';
        });
    };

    /* ── Dependencies panel ──────────────────────────────────────── */
    var loadingEl  = document.getElementById('marcus-deps-loading');
    var contentEl  = document.getElementById('marcus-deps-content');
    var errorEl    = document.getElementById('marcus-deps-error');
    var onList     = document.getElementById('marcus-deps-on');
    var blocksList = document.getElementById('marcus-deps-blocks');
    var relList    = document.getElementById('marcus-deps-relates');

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
            renderList(onList,      data.depends_on,  'No dependencies');
            renderList(blocksList,  data.blocks,       'Blocks nothing');
            renderList(relList,     data.relates_to,   'No related tickets');
        })
        .catch(function () {
            loadingEl.style.display = 'none';
            errorEl.style.display   = 'block';
            errorEl.textContent     = 'Could not reach Marcus at ' + LINKS_URL;
        });
}());
</script>
