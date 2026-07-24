<?php
/**
 * MarcusDevEnv board header template — injected via Kanboard's
 * 'template:project:header:after' hook, which fires on every
 * project-scoped view (board, list, calendar, Gantt, search), not just
 * the board — Kanboard has no board-only equivalent of this hook.
 *
 * Section 1 — Active AI Agents badge (polls /api/active-agents every 15 s)
 * Section 2 — Project Description link button
 * Section 3 — Project-level Human Gate / AI Gate toggle
 * Section 4 — AI Verify counter (only visible when AI Gate is active)
 *             Shows [−] N [+] where N is the number of required LLM review
 *             rounds before a ticket's branch is auto-merged.  0 = disabled.
 * Section 5 — Max dev environments counter (always visible, global —
 *             not scoped per project).  Shows [−] N [+] where N is the
 *             greatest number of "Open Dev Environment" Docker containers
 *             allowed to run at once across ALL tickets.  Once reached,
 *             starting a new one fails until an existing one is stopped.
 *             &#8734; (infinity) means no limit — the default until a
 *             human sets one here.
 *
 * The gate and verify_count settings persist via Marcus /api/gate-setting/project.
 * Default gate is "human"; default verify_count is 0.
 * Per-ticket overrides are in the task sidebar.
 * The max-dev-envs setting persists via Marcus /api/dev-env-setting.
 */
$marcusUrl        = getenv('MARCUS_URL') ?: 'http://localhost:4298';
// When Marcus requires bearer auth (MARCUS_AGENT_TOKEN set — remote-access
// mode), the browser must present the same token: fetch() calls send it as
// an Authorization header, plain navigation links carry ?token= (a link
// click cannot attach a header). Empty = auth disabled = omitted entirely.
$marcusToken      = getenv('MARCUS_AGENT_TOKEN') ?: '';
$apiUrl           = $marcusUrl . '/api/active-agents';
$projectId        = $project['id'] ?? '';
$descUrl          = $marcusUrl . '/project-description?project_id=' . urlencode((string) $projectId)
                  . ($marcusToken !== '' ? '&token=' . urlencode($marcusToken) : '');
$gateApiBase      = $marcusUrl . '/api/gate-setting';
$devEnvSettingUrl = $marcusUrl . '/api/dev-env-setting';
$projectRepoUrl   = $marcusUrl . '/api/project-repo?project_id=' . urlencode((string) $projectId);
$eventsStreamUrl  = $marcusUrl . '/api/events/stream'
    . ($marcusToken !== '' ? '?token=' . urlencode($marcusToken) : '');
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

/* ── Actively-worked ticket highlight ─────────────────────────────────── */
/* A golden ring marks the cards an AI agent is working RIGHT NOW — driven by
   Marcus's activity heartbeat (the agent reported progress in the last ~40s),
   NOT by ticket state, so a card stuck in the wrong column can't wrongly show
   or hide it. Rendered as a box-shadow ring rather than a real `border` so it
   doesn't shift the card's layout or fight Kanboard's own category-colored
   left border, and respects the card's rounded corners. */
.task-board.marcus-ai-active {
    border-color: #f5b301 !important;
    /* The ring itself comes from the animation below. A CSS animation
       outranks Kanboard's own (non-important) card box-shadow in the
       cascade, whereas an `!important` static box-shadow here would instead
       OUTRANK the animation and freeze the pulse — so the moving ring lives
       only in the keyframes, with a static fallback for reduced-motion. */
    animation: marcusAiPulse 2s ease-in-out infinite;
}
@keyframes marcusAiPulse {
    0%, 100% { box-shadow: 0 0 0 2px #f5b301, 0 0 6px 1px rgba(245, 179, 1, 0.35); }
    50%      { box-shadow: 0 0 0 2px #f5b301, 0 0 13px 3px rgba(245, 179, 1, 0.65); }
}
@media (prefers-reduced-motion: reduce) {
    .task-board.marcus-ai-active {
        animation: none;
        box-shadow: 0 0 0 2px #f5b301, 0 0 9px 2px rgba(245, 179, 1, 0.5) !important;
    }
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

    <!-- Gitea repository link (shown once the repo is provisioned) -->
    <a id="marcus-repo-link" href="#" target="_blank" rel="noopener noreferrer"
       style="display:none;align-items:center;gap:5px;padding:4px 10px;border-radius:12px;
              font-size:12px;font-weight:600;text-decoration:none;
              background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0;">
        &#128193; Repository
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

    <!-- Max parallel dev environments (global, always shown) -->
    <div id="marcus-devenv-wrap" style="display:inline-flex;align-items:center;gap:6px;">
        <span class="marcus-gate-label" title="Limits how many 'Open Dev Environment' Docker containers can run at once, across every ticket">Max dev environments:</span>
        <div class="marcus-verify-counter">
            <button class="marcus-verify-btn" id="marcus-devenv-dec"
                    onclick="adjustMaxDevEnvs(-1)" title="Decrease the limit">&#8722;</button>
            <span class="marcus-verify-val" id="marcus-devenv-val">&#8734;</span>
            <button class="marcus-verify-btn" id="marcus-devenv-inc"
                    onclick="adjustMaxDevEnvs(1)" title="Increase the limit">&#43;</button>
        </div>
        <span class="marcus-gate-saving" id="marcus-devenv-saving">saving&hellip;</span>
    </div>

</div>

<script>
(function () {
    var AGENTS_URL       = <?= json_encode($apiUrl) ?>;
    var GATE_URL         = <?= json_encode($gateApiBase) ?>;
    var DEV_ENV_SETTING_URL = <?= json_encode($devEnvSettingUrl) ?>;
    var PROJECT_REPO_URL = <?= json_encode($projectRepoUrl) ?>;
    var EVENTS_STREAM_URL = <?= json_encode($eventsStreamUrl) ?>;
    var PROJECT_ID       = <?= json_encode((int) $projectId) ?>;
    var MARCUS_TOKEN     = <?= json_encode($marcusToken) ?>;
    var INTERVAL         = 15000;

    // Every fetch below goes through this: attaches the bearer token when
    // Marcus requires auth (MARCUS_AGENT_TOKEN set), no-op otherwise.
    function marcusHeaders(extra) {
        var h = extra || {};
        if (MARCUS_TOKEN) { h['Authorization'] = 'Bearer ' + MARCUS_TOKEN; }
        return h;
    }

    /* ── Live board refresh (push, no polling) ───────────────────────── */
    // Hold ONE Server-Sent Events connection to Marcus. Marcus pushes a
    // "refresh" the instant it changes anything (comment, card move, state),
    // so the board updates with zero delay. Never reloads while you're
    // typing or a Kanboard form is open — that reload is deferred until you
    // stop (a purely local check; it never polls the server).
    (function () {
        if (!EVENTS_STREAM_URL || typeof EventSource === 'undefined') { return; }
        var pending = false;

        function userIsBusy() {
            var el = document.activeElement;
            if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT'
                       || el.isContentEditable)) { return true; }
            if (document.querySelector('#popover-container, .modal-box')) {
                return true;
            }
            return false;
        }
        function doRefresh() {
            if (userIsBusy()) { pending = true; return; }
            window.location.reload();
        }
        // EventSource auto-reconnects (server sends `retry:`); no polling here.
        var es = new EventSource(EVENTS_STREAM_URL);
        es.addEventListener('refresh', doRefresh);
        // Local-only: once you stop typing, apply any refresh that arrived.
        setInterval(function () {
            if (pending && !userIsBusy()) { pending = false; window.location.reload(); }
        }, 1000);
    }());

    /* ── Gitea repository link ───────────────────────────────────────── */
    // Reveal the "Repository" button only once the project's repo exists
    // (repo_web_url is null until provisioned). href assignment (not
    // innerHTML) keeps a crafted repo name from injecting markup.
    (function () {
        var repoLink = document.getElementById('marcus-repo-link');
        if (!repoLink) { return; }
        fetch(PROJECT_REPO_URL, { cache: 'no-store', headers: marcusHeaders() })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.repo_web_url) {
                    repoLink.href = data.repo_web_url;
                    repoLink.style.display = 'inline-flex';
                }
            })
            .catch(function () { /* leave hidden on error */ });
    }());

    /* ── Active agents badge ─────────────────────────────────────────── */
    var badge   = document.getElementById('marcus-agent-badge');
    var label   = document.getElementById('marcus-agent-label');
    var tooltip = document.getElementById('marcus-agent-tooltip');

    // Ticket ids an AI agent is working RIGHT NOW — from Marcus's liveness
    // signal (working_ticket_ids: tickets an agent has reported progress on in
    // the last ~40s), NOT from ticket state/column. That keeps the golden ring
    // a true "an agent is actively working this" indicator even if a state bug
    // leaves a card stuck in a column. The ring clears when the agent stops
    // reporting (finished, handed off, blocked, or went silent).
    var activeTicketIds = Object.create(null);

    // (Re)paint the golden ring onto exactly the active cards and strip it
    // from every other card. Idempotent — safe to call after each poll AND
    // whenever Kanboard redraws the board (its own auto-refresh replaces the
    // card DOM, which would otherwise drop our class). Marcus ticket ids are
    // the Kanboard task ids, compared as strings.
    function applyAgentBorders() {
        var cards = document.querySelectorAll('.task-board[data-task-id]');
        for (var i = 0; i < cards.length; i++) {
            var id = String(cards[i].getAttribute('data-task-id'));
            if (activeTicketIds[id]) {
                cards[i].classList.add('marcus-ai-active');
            } else {
                cards[i].classList.remove('marcus-ai-active');
            }
        }
    }

    function updateAgents() {
        fetch(AGENTS_URL, { cache: 'no-store', headers: marcusHeaders() })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                // Two distinct signals:
                //  connected = agents polling Marcus for work (incl. idle)
                //  active    = agents actually working a claimed ticket
                var connected = data.connected_agent_count || 0;
                var active    = data.active_agent_count || 0;
                var agents    = data.agents || [];
                badge.className = active > 0 ? 'active' : (connected > 0 ? 'idle' : 'idle');
                label.textContent = '🔌 ' + connected + ' connected · 🤖 '
                    + active + ' working';
                if (connected === 0 && active === 0) {
                    tooltip.innerHTML = 'No AI agents connected right now.';
                } else {
                    var rows = agents.map(function (a) {
                        var u = a.usage;
                        var usageStr = '';
                        if (u && (u.used != null || u.limit != null)) {
                            var lim = (u.limit == null) ? '∞' : u.limit;
                            usageStr = '&nbsp;&mdash;&nbsp;usage ' + (u.used == null ? '?' : u.used)
                                + ' / ' + lim + (u.unit ? ' ' + u.unit : '');
                        }
                        return '&#x25B6; Ticket&nbsp;<strong>#' + a.ticket_id
                            + '</strong>&nbsp;&mdash;&nbsp;' + a.agent_id + usageStr;
                    });
                    tooltip.innerHTML =
                        '<strong>' + connected + '</strong> connected, <strong>'
                        + active + '</strong> working<br>'
                        + (rows.length ? rows.join('<br>') : 'No claimed tickets.');
                }
                // Rebuild the active set from Marcus's activity-based liveness
                // signal (not ticket state) and repaint the golden rings.
                activeTicketIds = Object.create(null);
                (data.working_ticket_ids || []).forEach(function (id) {
                    activeTicketIds[String(id)] = true;
                });
                applyAgentBorders();
            })
            .catch(function () {
                badge.className   = 'error';
                label.textContent = '🤖 Marcus: unreachable';
                tooltip.innerHTML = 'Could not reach Marcus at<br>' + AGENTS_URL;
                // Leave whatever rings are currently shown — a transient
                // Marcus blip shouldn't flicker every card.
            });
    }
    updateAgents();
    setInterval(updateAgents, INTERVAL);

    // Kanboard periodically re-renders the board (its own AJAX auto-refresh),
    // which rebuilds the card DOM and would drop our class. Re-apply from the
    // cached active set whenever the board subtree changes. Debounced so a
    // burst of mutations triggers a single repaint.
    (function () {
        var boardEl = document.getElementById('board') || document.body;
        if (typeof MutationObserver === 'undefined') { return; }
        var scheduled = false;
        var obs = new MutationObserver(function () {
            if (scheduled) { return; }
            scheduled = true;
            setTimeout(function () { scheduled = false; applyAgentBorders(); }, 100);
        });
        obs.observe(boardEl, { childList: true, subtree: true });
    }());

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
    fetch(GATE_URL + '?project_id=' + PROJECT_ID, { cache: 'no-store', headers: marcusHeaders() })
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
            headers: marcusHeaders({ 'Content-Type': 'application/json' }),
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
            headers: marcusHeaders({ 'Content-Type': 'application/json' }),
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

    /* ── Max parallel dev environments (global) ────────────────────── */
    var devEnvSaving = document.getElementById('marcus-devenv-saving');
    var devEnvValEl  = document.getElementById('marcus-devenv-val');
    var devEnvDecBtn = document.getElementById('marcus-devenv-dec');
    var devEnvIncBtn = document.getElementById('marcus-devenv-inc');
    var INFINITY_CHAR = '∞';

    function applyMaxDevEnvs(value) {
        // value is null/undefined (unlimited) or a non-negative integer.
        if (value === null || value === undefined) {
            devEnvValEl.textContent = INFINITY_CHAR;
            devEnvValEl.className = 'marcus-verify-val';
            devEnvDecBtn.disabled = true; // nothing to decrement from unlimited
        } else {
            devEnvValEl.textContent = value;
            devEnvValEl.className = 'marcus-verify-val' + (value > 0 ? ' active' : '');
            devEnvDecBtn.disabled = (value <= 0);
        }
    }

    // Load the current global limit on page load.
    fetch(DEV_ENV_SETTING_URL, { cache: 'no-store', headers: marcusHeaders() })
        .then(function (r) { return r.json(); })
        .then(function (data) { applyMaxDevEnvs(data.max_parallel_containers); })
        .catch(function () { applyMaxDevEnvs(null); });

    window.adjustMaxDevEnvs = function (delta) {
        var curText = devEnvValEl.textContent;
        var cur = (curText === INFINITY_CHAR) ? null : (parseInt(curText, 10) || 0);
        var next;
        if (cur === null) {
            if (delta <= 0) { return; } // already unlimited; − is a no-op (button disabled)
            next = 1; // first explicit cap
        } else {
            next = Math.max(0, cur + delta);
            if (next === cur) { return; }
        }
        setMaxDevEnvs(next);
    };

    window.setMaxDevEnvs = function (count) {
        devEnvSaving.style.display = 'inline';
        devEnvDecBtn.disabled = devEnvIncBtn.disabled = true;

        fetch(DEV_ENV_SETTING_URL, {
            method: 'PUT',
            headers: marcusHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ max_parallel_containers: count }),
        })
        .then(function (r) { return r.json(); })
        .then(function () { applyMaxDevEnvs(count); })
        .catch(function () { /* keep current visual state */ })
        .finally(function () {
            devEnvIncBtn.disabled = false;
            devEnvDecBtn.disabled = (count <= 0);
            devEnvSaving.style.display = 'none';
        });
    };
}());
</script>
