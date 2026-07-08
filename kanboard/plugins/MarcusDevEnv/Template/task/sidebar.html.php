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
 *   AI Verify counter: when the effective gate is AI, shows [−] N [+] to
 *   set how many LLM review rounds run before the branch auto-merges.
 *   The reset button (↩) clears the per-ticket override and inherits
 *   from the project setting.
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

/* ── AI Verify counter (sidebar) ────────────────────────────────── */
.m-verify-section {
    margin-top: 8px;
    padding-top: 6px;
    border-top: 1px solid rgba(0,0,0,.08);
    display: none; /* shown only when effective gate is AI */
}
.m-verify-section.visible { display: block; }
.m-verify-desc {
    font-size: 10px;
    color: #888;
    margin: 0 0 6px;
    line-height: 1.4;
}
.m-verify-row {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
}
.m-verify-counter {
    display: inline-flex;
    align-items: center;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    overflow: hidden;
    background: #f9fafb;
}
.m-verify-btn {
    width: 22px;
    height: 22px;
    border: none;
    background: transparent;
    cursor: pointer;
    font-size: 14px;
    font-weight: 700;
    color: #6b7280;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.1s;
    line-height: 1;
}
.m-verify-btn:hover:not(:disabled) { background: #e5e7eb; }
.m-verify-btn:disabled { opacity: .4; cursor: default; }
.m-verify-val {
    padding: 0 6px;
    font-size: 12px;
    font-weight: 700;
    color: #9ca3af;
    min-width: 18px;
    text-align: center;
    user-select: none;
}
.m-verify-val.active { color: #7c3aed; }
.m-verify-rounds-label {
    font-size: 10px;
    color: #6b7280;
    white-space: nowrap;
}
.m-verify-reset-btn {
    border: none;
    background: #f3f4f6;
    border-radius: 4px;
    cursor: pointer;
    font-size: 11px;
    color: #6b7280;
    padding: 2px 5px;
    transition: background 0.1s;
    display: none;
}
.m-verify-reset-btn:hover { background: #e5e7eb; }
.m-verify-reset-btn:disabled { opacity: .4; cursor: default; }
.m-verify-inherit-row {
    margin-top: 3px;
    font-size: 10px;
    color: #9ca3af;
    width: 100%;
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

        <!-- AI Verify counter (only shown when effective gate is AI) -->
        <div class="m-verify-section" id="marcus-verify-section">
            <p class="m-verify-desc">
                <strong>AI Verify rounds</strong>: how many independent LLM reviews
                run before this ticket's branch is auto-merged.  0 = no verification.
            </p>
            <div class="m-verify-row">
                <div class="m-verify-counter">
                    <button class="m-verify-btn" id="marcus-tverify-dec"
                            onclick="adjustTicketVerify(-1)" title="Decrease verification rounds">&#8722;</button>
                    <span class="m-verify-val" id="marcus-tverify-val">0</span>
                    <button class="m-verify-btn" id="marcus-tverify-inc"
                            onclick="adjustTicketVerify(1)" title="Increase verification rounds">&#43;</button>
                </div>
                <span class="m-verify-rounds-label">rounds</span>
                <button class="m-verify-reset-btn" id="marcus-tverify-reset"
                        onclick="resetTicketVerify()" title="Inherit from project">&#8617;</button>
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
    var saving        = document.getElementById('marcus-tg-saving');
    var effEl         = document.getElementById('marcus-eff-gate');
    var verifySection = document.getElementById('marcus-verify-section');
    var verifySaving  = document.getElementById('marcus-verify-saving');
    var verifyNote    = document.getElementById('marcus-verify-inherit-note');
    var tVerifyDecBtn = document.getElementById('marcus-tverify-dec');
    var tVerifyIncBtn = document.getElementById('marcus-tverify-inc');
    var tVerifyValEl  = document.getElementById('marcus-tverify-val');
    var tVerifyReset  = document.getElementById('marcus-tverify-reset');

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

    // ticketVerifyCount: null = inheriting; number = per-ticket override
    function applyVerify(ticketVerifyCount, projectVerifyCount, effectiveVerifyCount) {
        var count = effectiveVerifyCount || 0;
        tVerifyValEl.textContent = count;
        tVerifyValEl.className = 'm-verify-val' + (count > 0 ? ' active' : '');
        tVerifyDecBtn.disabled = (count <= 0);

        // Show reset button only when there is a per-ticket override to clear
        var hasOverride = (ticketVerifyCount !== null && ticketVerifyCount !== undefined);
        tVerifyReset.style.display = hasOverride ? 'inline' : 'none';

        if (!hasOverride) {
            var src = (projectVerifyCount || 0) > 0
                ? 'project: ' + projectVerifyCount + (projectVerifyCount === 1 ? ' round' : ' rounds')
                : 'project: off';
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
                applyTicketPills(data.ticket_gate);
                applyEffective(data.effective || 'human');
                applyVerify(
                    data.ticket_verify_count,
                    data.project_verify_count || 0,
                    data.effective_verify_count || 0
                );
            })
            .catch(function () {
                applyProjectPills('human');
                applyTicketPills(null);
                applyEffective('human');
                applyVerify(null, 0, 0);
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

    window.adjustTicketVerify = function (delta) {
        var cur = parseInt(tVerifyValEl.textContent, 10) || 0;
        var next = Math.max(0, cur + delta);
        if (next === cur) { return; }
        setTicketVerify(next);
    };

    window.resetTicketVerify = function () {
        setTicketVerify(null);
    };

    window.setTicketVerify = function (count) {
        verifySaving.style.display = 'inline';
        tVerifyDecBtn.disabled = tVerifyIncBtn.disabled = tVerifyReset.disabled = true;

        fetch(GATE_URL + '/ticket', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ticket_id: TICKET_ID, verify_count: count }),
        })
        .then(function (r) { return r.json(); })
        .then(function () { loadGateSettings(); })
        .catch(function () { /* keep current visual state */ })
        .finally(function () {
            tVerifyDecBtn.disabled = false;
            tVerifyIncBtn.disabled = false;
            tVerifyReset.disabled = false;
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
