<?php
/**
 * MarcusDevEnv board header template — injected at the top of every board view.
 *
 * Section 1 — Active AI Agents badge (polls /api/active-agents every 15 s)
 * Section 2 — Project Description link button
 * Section 3 — Project-level Human Gate / AI Gate toggle
 * Section 4 — AI Verify counter (only visible when AI Gate is active)
 *             Shows [−] N [+] where N is the number of required LLM review
 *             rounds before a ticket's branch is auto-merged.  0 = disabled.
 *
 * The gate and verify_count settings persist via Marcus /api/gate-setting/project.
 * Default gate is "human"; default verify_count is 0.
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

/* ── AI Verify counter ────────────────────────────────────────────────── */
#marcus-verify-wrap {
    display: none; /* hidden by default; shown only when AI gate is active */
    align-items: center;
    gap: 6px;
}
#marcus-verify-wrap.visible { display: inline-flex; }
.marcus-verify-counter {
    display: inline-flex;
    align-items: center;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    overflow: hidden;
    background: #f9fafb;
}
.marcus-verify-btn {
    width: 26px;
    height: 26px;
    border: none;
    background: transparent;
    cursor: pointer;
    font-size: 15px;
    font-weight: 700;
    color: #6b7280;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.12s, color 0.12s;
    line-height: 1;
}
.marcus-verify-btn:hover:not(:disabled) { background: #e5e7eb; }
.marcus-verify-btn:disabled { opacity: 0.4; cursor: default; }
.marcus-verify-val {
    padding: 0 8px;
    font-size: 13px;
    font-weight: 700;
    color: #9ca3af;
    min-width: 22px;
    text-align: center;
    user-select: none;
}
.marcus-verify-val.active { color: #7c3aed; }
.marcus-verify-rounds-label {
    font-size: 11px;
    color: #6b7280;
    white-space: nowrap;
}
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

    <!-- AI Verify counter (only shown when AI Gate is active) -->
    <div id="marcus-verify-wrap">
        <span class="marcus-gate-label">AI Verify:</span>
        <div class="marcus-verify-counter">
            <button class="marcus-verify-btn" id="marcus-verify-dec"
                    onclick="adjustProjectVerify(-1)" title="Decrease verification rounds">&#8722;</button>
            <span class="marcus-verify-val" id="marcus-verify-val">0</span>
            <button class="marcus-verify-btn" id="marcus-verify-inc"
                    onclick="adjustProjectVerify(1)" title="Increase verification rounds">&#43;</button>
        </div>
        <span class="marcus-verify-rounds-label">rounds</span>
        <span class="marcus-gate-saving" id="marcus-verify-saving">saving&hellip;</span>
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

    /* ── Project gate + verify counter ─────────────────────────────── */
    var saving      = document.getElementById('marcus-gate-saving');
    var verifySaving = document.getElementById('marcus-verify-saving');
    var verifyWrap  = document.getElementById('marcus-verify-wrap');
    var verifyValEl = document.getElementById('marcus-verify-val');
    var verifyDecBtn = document.getElementById('marcus-verify-dec');
    var verifyIncBtn = document.getElementById('marcus-verify-inc');

    function applyProjectGate(gate) {
        var humanBtn = document.getElementById('pgBtn-human');
        var aiBtn    = document.getElementById('pgBtn-ai');
        humanBtn.className = gate === 'human' ? 'active-human' : '';
        aiBtn.className    = gate === 'ai'    ? 'active-ai'    : '';
        // Show/hide AI Verify counter depending on gate
        if (gate === 'ai') {
            verifyWrap.classList.add('visible');
        } else {
            verifyWrap.classList.remove('visible');
        }
    }

    function applyProjectVerify(count) {
        var n = count || 0;
        verifyValEl.textContent = n;
        verifyValEl.className = 'marcus-verify-val' + (n > 0 ? ' active' : '');
        verifyDecBtn.disabled = (n <= 0);
    }

    // Load current project gate + verify_count on page load
    fetch(GATE_URL + '?project_id=' + PROJECT_ID, { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            applyProjectGate(data.project_gate || 'human');
            applyProjectVerify(data.project_verify_count || 0);
        })
        .catch(function () {
            applyProjectGate('human');
            applyProjectVerify(0);
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

    window.adjustProjectVerify = function (delta) {
        var cur = parseInt(verifyValEl.textContent, 10) || 0;
        var next = Math.max(0, cur + delta);
        if (next === cur) { return; }
        setProjectVerify(next);
    };

    window.setProjectVerify = function (count) {
        verifySaving.style.display = 'inline';
        verifyDecBtn.disabled = verifyIncBtn.disabled = true;

        fetch(GATE_URL + '/project', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_id: PROJECT_ID,
                gate: document.getElementById('pgBtn-ai').classList.contains('active-ai') ? 'ai' : 'human',
                verify_count: count,
            }),
        })
        .then(function (r) { return r.json(); })
        .then(function () { applyProjectVerify(count); })
        .catch(function () { /* keep current visual state */ })
        .finally(function () {
            verifyDecBtn.disabled = (parseInt(verifyValEl.textContent, 10) || 0) <= 0;
            verifyIncBtn.disabled = false;
            verifySaving.style.display = 'none';
        });
    };
}());
</script>
