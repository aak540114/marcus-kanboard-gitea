<?php
/**
 * MarcusDevEnv board header template — injected at the top of every board view.
 *
 * Section 1 — Active AI Agents badge (polls /api/active-agents every 15 s)
 * Section 2 — Project Description link button
 * Section 3 — Project-level Human Gate / AI Gate toggle
 * Section 4 — AI Verify toggle (only visible when AI Gate is active)
 *
 * The gate and verify toggles persist via Marcus /api/gate-setting/project.
 * Default gate is "human"; default verify is false.
 * Per-ticket overrides are in the task sidebar.
 */
$marcusUrl   = getenv('MARCUS_URL') ?: 'http://localhost:4298';
$apiUrl      = $marcusUrl . '/api/active-agents';
$projectId   = $project['id'] ?? '';
$descUrl     = $marcusUrl . '/project-description?project_id=' . urlencode((string) $projectId);
$gateApiBase = $marcusUrl . '/api/gate-setting';
?>
<style>
/* ── Active agents badge ──────────────────────────────────────────────── */
#marcus-agent-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    cursor: default;
    transition: background 0.3s, color 0.3s;
    border: 1px solid transparent;
}
#marcus-agent-badge.active { background:#e6f4ea; color:#1a7f3c; border-color:#a8d5b5; }
#marcus-agent-badge.idle   { background:#f4f4f4; color:#888;    border-color:#ddd;    }
#marcus-agent-badge.error  { background:#fff3e0; color:#b45309; border-color:#f8c97a; }
#marcus-agent-badge .badge-dot {
    width:7px; height:7px; border-radius:50%; flex-shrink:0;
}
#marcus-agent-badge.active .badge-dot { background:#1a7f3c; }
#marcus-agent-badge.idle   .badge-dot { background:#aaa;    }
#marcus-agent-badge.error  .badge-dot { background:#b45309; }
#marcus-agent-tooltip {
    display:none; position:absolute; z-index:9999;
    background:#1e2533; color:#e8eaf0;
    border-radius:6px; padding:8px 12px;
    font-size:12px; line-height:1.6; white-space:nowrap;
    box-shadow:0 4px 16px rgba(0,0,0,.25);
    pointer-events:none; margin-top:4px;
}
#marcus-agent-badge:hover + #marcus-agent-tooltip,
#marcus-agent-badge:focus + #marcus-agent-tooltip { display:block; }

/* ── Gate toggle ─────────────────────────────────────────────────────── */
.marcus-gate-wrap {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 600;
}
.marcus-gate-label {
    color: #666;
    font-size: 11px;
    white-space: nowrap;
}
.marcus-gate-toggle {
    display: inline-flex;
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid #d1d5db;
    background: #f3f4f6;
}
.marcus-gate-toggle button {
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
    border: none;
    cursor: pointer;
    background: transparent;
    color: #6b7280;
    transition: background 0.15s, color 0.15s;
    white-space: nowrap;
}
.marcus-gate-toggle button.active-human {
    background: #dbeafe;
    color: #1d4ed8;
}
.marcus-gate-toggle button.active-ai {
    background: #f3e8ff;
    color: #7c3aed;
}
.marcus-gate-toggle button:disabled {
    opacity: 0.5;
    cursor: default;
}
.marcus-gate-saving {
    font-size: 10px;
    color: #9ca3af;
    margin-left: 4px;
    display: none;
}

/* ── AI Verify toggle ────────────────────────────────────────────────── */
#marcus-verify-wrap {
    display: none; /* hidden by default; shown only when AI gate is active */
    align-items: center;
    gap: 6px;
}
#marcus-verify-wrap.visible { display: inline-flex; }
.marcus-verify-switch {
    position: relative;
    display: inline-block;
    width: 36px;
    height: 20px;
}
.marcus-verify-switch input { opacity: 0; width: 0; height: 0; }
.marcus-verify-slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background: #d1d5db;
    border-radius: 20px;
    transition: background 0.2s;
}
.marcus-verify-slider:before {
    content: '';
    position: absolute;
    width: 14px; height: 14px;
    left: 3px; bottom: 3px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s;
}
.marcus-verify-switch input:checked + .marcus-verify-slider { background: #7c3aed; }
.marcus-verify-switch input:checked + .marcus-verify-slider:before { transform: translateX(16px); }
.marcus-verify-label {
    font-size: 11px;
    font-weight: 600;
    color: #6b7280;
    white-space: nowrap;
}
.marcus-verify-label.on { color: #7c3aed; }
</style>

<div style="padding: 0 16px 2px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">

    <!-- Active agents badge -->
    <div style="position: relative; display: inline-block;">
        <span id="marcus-agent-badge" class="idle" title="">
            <span class="badge-dot"></span>
            <span id="marcus-agent-label">&#129302; Marcus: checking&hellip;</span>
        </span>
        <div id="marcus-agent-tooltip"></div>
    </div>

    <!-- Project Description link -->
    <a href="<?= htmlspecialchars($descUrl) ?>" target="_blank" rel="noopener noreferrer"
       style="display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:12px;
              font-size:12px;font-weight:600;text-decoration:none;
              background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;">
        &#128196; Project Description
    </a>

    <!-- Project-level gate toggle -->
    <div class="marcus-gate-wrap">
        <span class="marcus-gate-label">Project gate:</span>
        <div class="marcus-gate-toggle" id="marcus-project-gate">
            <button id="pgBtn-human" onclick="setProjectGate('human')" title="AI waits for human review before marking done">
                &#128100; Human Gate
            </button>
            <button id="pgBtn-ai" onclick="setProjectGate('ai')" title="AI works autonomously from ready to done">
                &#129302; AI Gate
            </button>
        </div>
        <span class="marcus-gate-saving" id="marcus-gate-saving">saving&hellip;</span>
    </div>

    <!-- AI Verify toggle (only shown when AI Gate is active) -->
    <div id="marcus-verify-wrap">
        <span class="marcus-gate-label">AI Verify:</span>
        <label class="marcus-verify-switch" title="When on, a second AI agent reviews the work before merging">
            <input type="checkbox" id="marcus-verify-chk" onchange="setProjectVerify(this.checked)">
            <span class="marcus-verify-slider"></span>
        </label>
        <span class="marcus-verify-label" id="marcus-verify-label">Off</span>
    </div>

</div>

<script>
(function () {
    var AGENTS_URL  = <?= json_encode($apiUrl) ?>;
    var GATE_URL    = <?= json_encode($gateApiBase) ?>;
    var PROJECT_ID  = <?= json_encode((int) $projectId) ?>;
    var INTERVAL    = 15000;

    /* ── Active agents badge ─────────────────────────────────────────── */
    var badge   = document.getElementById('marcus-agent-badge');
    var label   = document.getElementById('marcus-agent-label');
    var tooltip = document.getElementById('marcus-agent-tooltip');

    function updateAgents() {
        fetch(AGENTS_URL, { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var count  = data.active_agent_count || 0;
                var agents = data.agents || [];
                badge.className = count > 0 ? 'active' : 'idle';
                if (count === 0) {
                    label.textContent = '🤖 Marcus: no active agents';
                    tooltip.innerHTML = 'No AI agents are working right now.';
                } else {
                    label.textContent = '🤖 Marcus: ' + count
                        + (count === 1 ? ' agent active' : ' agents active');
                    tooltip.innerHTML = agents.map(function (a) {
                        return '&#x25B6; Ticket&nbsp;<strong>#' + a.ticket_id
                            + '</strong>&nbsp;&mdash;&nbsp;' + a.agent_id;
                    }).join('<br>');
                }
            })
            .catch(function () {
                badge.className   = 'error';
                label.textContent = '🤖 Marcus: unreachable';
                tooltip.innerHTML = 'Could not reach Marcus at<br>' + AGENTS_URL;
            });
    }
    updateAgents();
    setInterval(updateAgents, INTERVAL);

    /* ── Project gate + verify toggle ───────────────────────────────── */
    var saving      = document.getElementById('marcus-gate-saving');
    var verifyWrap  = document.getElementById('marcus-verify-wrap');
    var verifyChk   = document.getElementById('marcus-verify-chk');
    var verifyLabel = document.getElementById('marcus-verify-label');

    function applyProjectGate(gate) {
        var humanBtn = document.getElementById('pgBtn-human');
        var aiBtn    = document.getElementById('pgBtn-ai');
        humanBtn.className = gate === 'human' ? 'active-human' : '';
        aiBtn.className    = gate === 'ai'    ? 'active-ai'    : '';
        // Show/hide AI Verify toggle depending on gate
        if (gate === 'ai') {
            verifyWrap.classList.add('visible');
        } else {
            verifyWrap.classList.remove('visible');
        }
    }

    function applyProjectVerify(verify) {
        verifyChk.checked = !!verify;
        verifyLabel.textContent = verify ? 'On' : 'Off';
        verifyLabel.className = 'marcus-verify-label' + (verify ? ' on' : '');
    }

    // Load current project gate + verify on page load
    fetch(GATE_URL + '?project_id=' + PROJECT_ID, { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            applyProjectGate(data.project_gate || 'human');
            applyProjectVerify(!!data.project_verify);
        })
        .catch(function () {
            applyProjectGate('human');
            applyProjectVerify(false);
        });

    window.setProjectGate = function (gate) {
        saving.style.display = 'inline';
        var humanBtn = document.getElementById('pgBtn-human');
        var aiBtn    = document.getElementById('pgBtn-ai');
        humanBtn.disabled = aiBtn.disabled = true;

        fetch(GATE_URL + '/project', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project_id: PROJECT_ID, gate: gate }),
        })
        .then(function (r) { return r.json(); })
        .then(function () { applyProjectGate(gate); })
        .catch(function () { /* keep current visual state */ })
        .finally(function () {
            humanBtn.disabled = aiBtn.disabled = false;
            saving.style.display = 'none';
        });
    };

    window.setProjectVerify = function (enabled) {
        saving.style.display = 'inline';
        verifyChk.disabled = true;

        fetch(GATE_URL + '/project', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            // Also send the current gate so the PUT handler has both fields
            body: JSON.stringify({
                project_id: PROJECT_ID,
                gate: document.getElementById('pgBtn-ai').classList.contains('active-ai') ? 'ai' : 'human',
                verify: enabled,
            }),
        })
        .then(function (r) { return r.json(); })
        .then(function () { applyProjectVerify(enabled); })
        .catch(function () { /* keep current visual state */ })
        .finally(function () {
            verifyChk.disabled = false;
            saving.style.display = 'none';
        });
    };
}());
</script>
