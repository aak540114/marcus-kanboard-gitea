<?php
/**
 * MarcusDevEnv board header template — injected via Kanboard's
 * 'template:project:header:after' hook, which fires on every
 * project-scoped view (board, list, calendar, Gantt, search), not just
 * the board — Kanboard has no board-only equivalent of this hook.
 *
 * Section 1 — Active AI Agents badge (polls /api/active-agents every 15 s)
 * Section 2 — Project Description link button
 * Section 2b — Project Stats link button (tickets/hour into Done and
 *              Waiting for Human, plus main-branch line count — see
 *              /project-stats and src/core/project_stats.py)
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
 * Section 6 — Main-branch preview Start/Stop buttons. Project-level (not
 *             per-ticket) — deploys the project's `main` branch instead of
 *             a ticket's branch, so a human can preview what's currently
 *             live/merged. Auto-reloads on every push to main (a ticket
 *             merge or a direct push), same as a ticket preview reloads on
 *             every push to its own branch. Counts against the same Max
 *             dev environments limit above (Section 5) — no separate
 *             reservation.
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
$statsUrl         = $marcusUrl . '/project-stats?project_id=' . urlencode((string) $projectId)
                  . ($marcusToken !== '' ? '&token=' . urlencode($marcusToken) : '');
$gateApiBase      = $marcusUrl . '/api/gate-setting';
$projectEnabledUrl = $marcusUrl . '/api/project-enabled';
$decomposeSettingUrl = $marcusUrl . '/api/decompose-setting';
$devEnvSettingUrl = $marcusUrl . '/api/dev-env-setting';
$projectRepoUrl   = $marcusUrl . '/api/project-repo?project_id=' . urlencode((string) $projectId);
// Carry the token in the query string (not a header) so the instant
// new-project signal stays a CORS-simple GET — no preflight — even under
// MARCUS_AGENT_TOKEN, exactly like $descUrl / $eventsStreamUrl.
$projectSeenUrl   = $marcusUrl . '/api/project-seen?project_id=' . urlencode((string) $projectId)
                  . ($marcusToken !== '' ? '&token=' . urlencode($marcusToken) : '');
$eventsStreamUrl  = $marcusUrl . '/api/events/stream'
    . ($marcusToken !== '' ? '?token=' . urlencode($marcusToken) : '');
$devEnvMainViewUrl = $marcusUrl . '/dev-env/main/view'
                  . '?project_id=' . urlencode((string) $projectId)
                  . '&provider='   . urlencode('kanboard')
                  . ($marcusToken !== '' ? '&token=' . urlencode($marcusToken) : '');
$devEnvMainStopUrl = $marcusUrl . '/dev-env/main/stop'
                  . '?project_id=' . urlencode((string) $projectId)
                  . '&provider='   . urlencode('kanboard');
$devEnvMainStatusUrl = $marcusUrl . '/api/dev-env/main/status'
                  . '?project_id=' . urlencode((string) $projectId)
                  . '&provider='   . urlencode('kanboard');
// /dev-env/logs is generic over ticket_id — the main-branch preview's
// DevEnvironmentManager identity is the synthetic "main-<project_id>"
// (see _main_preview_ticket_id in server.py), so it needs no dedicated
// route of its own, just this one query param.
$devEnvMainLogsUrl = $marcusUrl . '/dev-env/logs'
                  . '?ticket_id=' . urlencode('main-' . (string) $projectId)
                  . '&provider='  . urlencode('kanboard')
                  . ($marcusToken !== '' ? '&token=' . urlencode($marcusToken) : '');
$cloneProjectUrl       = $marcusUrl . '/api/clone-project';
$cloneProjectStatusUrl = $marcusUrl . '/api/clone-project-status';
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

/* ── Project access toggle (master switch) ──────────────────────────────
   Whether Marcus — and any AI agent — is allowed to touch THIS project's
   tickets at all. Default OFF: a project a human hasn't explicitly
   enabled gets no Gitea repo, no column reconciliation, no claimed
   tickets. Separate from (and shown before) the Human/AI Gate toggle
   below, which only governs HOW Marcus works once it's already allowed
   to. */
.marcus-access-toggle {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 700;
    border: 1px solid;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.marcus-access-toggle.on {
    background: #f0fdf4;
    color: #15803d;
    border-color: #86efac;
}
.marcus-access-toggle.off {
    background: #fef2f2;
    color: #b91c1c;
    border-color: #fca5a5;
}
.marcus-access-toggle:disabled {
    opacity: 0.6;
    cursor: default;
}

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

/* ── Claimed ticket highlight ─────────────────────────────────────────── */
/* A golden ring marks every card an AI agent currently holds a CLAIM on —
   driven by Marcus's lifecycle record (ai_agent_id set AND state ==
   in_progress — see the filter below), not an activity heartbeat, so it
   stays lit for the whole time a ticket is claimed, not just while the
   agent is actively reporting progress. Marcus only ever holds a claim
   while a ticket sits in In Progress (see the claim invariant in
   HumanGatedWorkflow._on_status_changed) and releases it the moment the
   card leaves that column — to Ready, Done, Blocked, anywhere — whether
   moved by a human or by Marcus itself — so the ring disappears on the
   next poll below. Rendered as a box-shadow ring rather than a real
   `border` so it doesn't shift the card's layout or fight Kanboard's own
   category-colored left border, and respects the card's rounded corners. */
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

/* ── Main-branch preview buttons ─────────────────────────────────────── */
.marcus-main-preview-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-decoration: none;
    border: 1px solid;
    cursor: pointer;
}
.marcus-main-preview-btn.start {
    background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe;
}
.marcus-main-preview-btn.open {
    background: #f0fdf4; color: #15803d; border-color: #bbf7d0;
}
.marcus-main-preview-btn.stop {
    background: #fef2f2; color: #b91c1c; border-color: #fca5a5;
}
.marcus-main-preview-btn.logs {
    background: #f9fafb; color: #374151; border-color: #d1d5db;
}
.marcus-main-preview-btn:disabled { opacity: 0.6; cursor: default; }

/* ── Controls row ─────────────────────────────────────────────────────
   Kanboard's own project header (.project-header, in its project.css)
   lays out the project dropdown, view switcher, and search/filter box
   with FLOAT, not flexbox — .dropdown-component/.views-switcher-component
   float left, .filter-box-component floats too. An un-cleared block
   placed after them (which is what this row was, with only inline
   styles) only gets pushed down as far as needed to clear whichever
   float is tallest AT THAT HORIZONTAL POSITION — in practice, wherever
   the search box happens to end, which reads as "randomly starts below
   the search input". `clear: both` instead puts this row deterministically
   on its own new line below the ENTIRE header row every time, flush left.
   `flex-wrap: nowrap` + a thin horizontal scrollbar (instead of wrapping)
   keeps every control on one line even on a narrow viewport. */
#marcus-controls-row {
    clear: both;
    padding: 8px 16px 2px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: thin;
    scrollbar-color: #c7ccd4 transparent;
}
#marcus-controls-row::-webkit-scrollbar { height: 6px; }
#marcus-controls-row::-webkit-scrollbar-track { background: transparent; }
#marcus-controls-row::-webkit-scrollbar-thumb {
    background: #c7ccd4;
    border-radius: 3px;
}
#marcus-controls-row::-webkit-scrollbar-thumb:hover { background: #9aa1ac; }
#marcus-controls-row > * { flex-shrink: 0; }

/* ── Independent per-column scrolling ────────────────────────────────
   Kanboard renders the whole board as one table, and by default a
   column with many cards grows the WHOLE PAGE — scrolling down to see
   the rest of that column scrolls every other column's cards out of
   view too. Capping each column's own task-list (.board-task-list,
   Kanboard's own class — one per column per swimlane) to a max-height
   with its own scrollbar keeps every column independently reachable.
   The column header is a SEPARATE table row above .board-task-list
   (see table_column.php vs. table_tasks.php in Kanboard's source), so
   it naturally stays visible without needing sticky positioning — it
   was never part of what scrolls. The max-height below is a fallback
   for when JS is unavailable; applyColumnScrollHeights() (see the
   <script> block) replaces it with a precisely measured value on load,
   resize, and every board refresh. */
.board-task-list {
    max-height: calc(100vh - 320px);
    min-height: 60px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: #c7ccd4 transparent;
}
.board-task-list::-webkit-scrollbar { width: 6px; }
.board-task-list::-webkit-scrollbar-track { background: transparent; }
.board-task-list::-webkit-scrollbar-thumb {
    background: #c7ccd4;
    border-radius: 3px;
}
.board-task-list::-webkit-scrollbar-thumb:hover { background: #9aa1ac; }
</style>

<div id="marcus-controls-row">

    <!-- Project access toggle (master switch) -->
    <button id="marcus-access-toggle" class="marcus-access-toggle off"
            onclick="toggleProjectAccess()" disabled
            title="Whether Marcus and AI agents may work on this project's tickets at all">
        &#128274; Marcus: checking&hellip;
    </button>

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

    <!-- Project Stats link -->
    <a href="<?= htmlspecialchars($statsUrl) ?>" target="_blank" rel="noopener noreferrer"
       style="display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:12px;
              font-size:12px;font-weight:600;text-decoration:none;
              background:#ecfeff;color:#0e7490;border:1px solid #a5f3fc;">
        &#128202; Project Stats
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

    <!-- Project-level auto-decompose toggle -->
    <button id="marcus-decompose-toggle" class="marcus-access-toggle off"
            onclick="toggleProjectDecompose()" disabled
            title="Whether Marcus may automatically split a large ticket in this project into linked sub-tickets">
        &#129517; Decompose: checking&hellip;
    </button>

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

    <!-- Project-level main-branch preview (Start/Stop) -->
    <span id="marcus-main-preview-wrap" style="display:inline-flex;align-items:center;gap:6px;">
        <span style="font-size:12px;color:#aaa;">Checking main preview&hellip;</span>
    </span>

    <!-- Clone this project -->
    <span style="display:inline-flex;align-items:center;gap:6px;">
        <button id="marcus-clone-project-btn" class="marcus-main-preview-btn start"
                onclick="cloneThisProject()"
                title="Create a new project under a new name that replicates every ticket, the project description, settings, and the git repository of this project">
            &#128203; Clone this project
        </button>
        <span id="marcus-clone-status" style="font-size:11px;color:#6b7280;"></span>
    </span>

</div>

<script>
(function () {
    var AGENTS_URL       = <?= json_encode($apiUrl) ?>;
    var GATE_URL         = <?= json_encode($gateApiBase) ?>;
    var PROJECT_ENABLED_URL = <?= json_encode($projectEnabledUrl) ?>;
    var DECOMPOSE_SETTING_URL = <?= json_encode($decomposeSettingUrl) ?>;
    var DEV_ENV_SETTING_URL = <?= json_encode($devEnvSettingUrl) ?>;
    var PROJECT_REPO_URL = <?= json_encode($projectRepoUrl) ?>;
    var PROJECT_SEEN_URL = <?= json_encode($projectSeenUrl) ?>;
    var EVENTS_STREAM_URL = <?= json_encode($eventsStreamUrl) ?>;
    var DEV_ENV_MAIN_VIEW_URL   = <?= json_encode($devEnvMainViewUrl) ?>;
    var DEV_ENV_MAIN_STOP_URL   = <?= json_encode($devEnvMainStopUrl) ?>;
    var DEV_ENV_MAIN_STATUS_URL = <?= json_encode($devEnvMainStatusUrl) ?>;
    var DEV_ENV_MAIN_LOGS_URL   = <?= json_encode($devEnvMainLogsUrl) ?>;
    var CLONE_PROJECT_URL        = <?= json_encode($cloneProjectUrl) ?>;
    var CLONE_PROJECT_STATUS_URL = <?= json_encode($cloneProjectStatusUrl) ?>;
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

    // HTML-escape any value before putting it into innerHTML. Used for
    // agent-controlled fields (agent id, reported usage) so a crafted value
    // can't inject markup/script into the board.
    function mEsc(s) {
        return String(s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    }

    /* ── Independent per-column scroll height ───────────────────────────
       The CSS max-height on .board-task-list (above) is a rough fallback;
       this replaces it with the ACTUAL room available below each column
       in the current viewport, so a column's scrollbar starts exactly
       where the viewport ends rather than a guessed constant — accurate
       regardless of how tall Marcus's own header rows end up (which
       varies with viewport width, since #marcus-controls-row can wrap the
       overall header taller on a narrow screen even though it no longer
       wraps internally). Re-run on load, on resize, and after every board
       DOM mutation (Kanboard's own periodic AJAX refresh rebuilds the
       column markup, which would otherwise drop the inline height) —
       piggybacks on the MutationObserver already watching the board for
       the golden-ring feature, so this doesn't need a second observer. */
    function applyColumnScrollHeights() {
        var lists = document.querySelectorAll('.board-task-list');
        var vh = window.innerHeight;
        for (var i = 0; i < lists.length; i++) {
            var top = lists[i].getBoundingClientRect().top;
            var available = vh - top - 16;
            lists[i].style.maxHeight = Math.max(120, available) + 'px';
        }
    }
    // This <script> tag is rendered INSIDE the header hook, which sits
    // before the board table in the document (see Kanboard's
    // board/view_private.php: projectHeader renders first, then
    // table_container) — .board-task-list doesn't exist in the DOM yet
    // at the point this synchronous script runs, so an immediate call
    // here would be a silent no-op. DOMContentLoaded guarantees the full
    // board table has been parsed first; this script is not deferred/
    // async, so it always registers before that event can fire.
    document.addEventListener('DOMContentLoaded', applyColumnScrollHeights);
    window.addEventListener('resize', applyColumnScrollHeights);

    /* ── Project access toggle (master switch) ─────────────────────────
       Whether Marcus/AI agents may work on THIS project's tickets at
       all. Independent of — and loaded/toggled separately from — the
       Human/AI Gate toggle below. Default OFF until a human opts in. */
    (function () {
        var btn = document.getElementById('marcus-access-toggle');
        if (!btn || !PROJECT_ID) { return; }

        function render(enabled) {
            btn.classList.toggle('on', enabled);
            btn.classList.toggle('off', !enabled);
            btn.innerHTML = enabled
                ? '&#128275; Marcus: ON for this project'
                : '&#128274; Marcus: OFF for this project';
            btn.title = enabled
                ? 'Marcus and AI agents may claim and work this project\'s tickets. Click to disable.'
                : 'Marcus will not touch this project\'s tickets (no repo, no claims, no agents). Click to enable.';
        }

        fetch(PROJECT_ENABLED_URL + '?project_id=' + PROJECT_ID, {
            cache: 'no-store', headers: marcusHeaders(),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                render(!!data.enabled);
                btn.disabled = false;
            })
            .catch(function () {
                render(false);
                btn.disabled = false;
            });

        window.toggleProjectAccess = function () {
            var next = !btn.classList.contains('on');
            btn.disabled = true;
            fetch(PROJECT_ENABLED_URL, {
                method: 'PUT',
                headers: marcusHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ project_id: PROJECT_ID, enabled: next }),
            })
                .then(function (r) { return r.json(); })
                .then(function () { render(next); })
                .catch(function () { /* keep current visual state */ })
                .finally(function () { btn.disabled = false; });
        };
    })();

    /* ── Auto-decompose toggle ──────────────────────────────────────────
       Whether Marcus may automatically split a large ticket in THIS
       project into linked sub-tickets — whether triggered automatically
       (a large ready ticket) or via the "@marcus decompose" comment
       command; both respect this switch. Defaults ON (matching
       decomposition's behavior before this setting existed), unlike the
       Project access toggle above, which defaults OFF. */
    (function () {
        var btn = document.getElementById('marcus-decompose-toggle');
        if (!btn || !PROJECT_ID) { return; }

        function render(enabled) {
            btn.classList.toggle('on', enabled);
            btn.classList.toggle('off', !enabled);
            btn.innerHTML = enabled
                ? '&#129517; Decompose: ON'
                : '&#129517; Decompose: OFF';
            btn.title = enabled
                ? 'Marcus may split large tickets in this project into sub-tickets. Click to disable.'
                : 'Marcus will never split any ticket in this project into sub-tickets. Click to enable.';
        }

        fetch(DECOMPOSE_SETTING_URL + '?project_id=' + PROJECT_ID, {
            cache: 'no-store', headers: marcusHeaders(),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                render(data.decompose_enabled !== false);
                btn.disabled = false;
            })
            .catch(function () {
                render(true);
                btn.disabled = false;
            });

        window.toggleProjectDecompose = function () {
            var next = !btn.classList.contains('on');
            btn.disabled = true;
            fetch(DECOMPOSE_SETTING_URL, {
                method: 'PUT',
                headers: marcusHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ project_id: PROJECT_ID, decompose_enabled: next }),
            })
                .then(function (r) { return r.json(); })
                .then(function () { render(next); })
                .catch(function () { /* keep current visual state */ })
                .finally(function () { btn.disabled = false; });
        };
    })();

    /* ── Instant new-project signal (push, replaces the poll) ─────────────
       Kanboard has no server-side "project created" event or webhook, so the
       moment a human opens a project page — which Kanboard does immediately
       after creating one — tell Marcus this project exists. Marcus then
       creates its Gitea repo + reconciles its columns right away instead of
       waiting for its slow backstop poll. Idempotent on Marcus's side (a
       no-op once the project is provisioned). Header-less GET with the token
       in the query string, so it stays a CORS-simple request (no preflight)
       even under MARCUS_AGENT_TOKEN. */
    (function () {
        if (!PROJECT_ID || !PROJECT_SEEN_URL) { return; }
        try {
            fetch(PROJECT_SEEN_URL, { cache: 'no-store' })
                .catch(function () { /* backstop poll will catch it */ });
        } catch (e) { /* never let this break the header */ }
    })();

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
            // Regression: this used to check "#popover-container, .modal-box"
            // — #popover-container matches nothing anywhere in Kanboard (zero
            // occurrences of "popover" in its whole compiled assets/js/app.min.js,
            // verified directly against the v1.2.53 tag docker-compose.yml
            // pins), and .modal-box is a CLASS selector when Kanboard's own
            // modal system (KB.modal, same file) actually builds an ID:
            // create()/destroy() append/remove #modal-overlay (wrapping
            // #modal-box) around EVERY modal — "Add a new task", editing a
            // ticket, bulk actions, confirm dialogs, all of it. Neither old
            // selector could ever match, so a live refresh only got deferred
            // when focus happened to be literally inside a text field at that
            // exact instant — an open "new task" dialog with focus anywhere
            // else (a dropdown just clicked, a pause between keystrokes) got
            // silently wiped by the reload below. #modal-overlay is present
            // for the modal's entire lifetime, so this now actually holds.
            if (document.querySelector('#modal-overlay')) {
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

    // Ticket ids an AI agent currently holds a CLAIM on — from data.agents
    // (every lifecycle record with ai_agent_id set), NOT the narrower
    // activity-heartbeat signal (data.working_ticket_ids, still used above
    // for the badge's own "N working" count). Marcus only ever holds a
    // claim while the ticket sits in In Progress and releases it
    // immediately on any other column move, so this stays lit for the
    // ticket's whole claimed lifetime and clears within one poll of the
    // claim actually being released — whether that happens because the
    // agent finished, or because the card was dragged elsewhere. Filtered
    // to state === "in_progress" defensively here too, in case a claim
    // is ever observed outside that column for a moment (a Marcus-side
    // invariant, not something this display should just trust blindly).
    var claimedTicketIds = Object.create(null);

    // (Re)paint the golden ring onto exactly the claimed cards and strip it
    // from every other card. Idempotent — safe to call after each poll AND
    // whenever Kanboard redraws the board (its own auto-refresh replaces the
    // card DOM, which would otherwise drop our class). Marcus ticket ids are
    // the Kanboard task ids, compared as strings.
    function applyAgentBorders() {
        var cards = document.querySelectorAll('.task-board[data-task-id]');
        for (var i = 0; i < cards.length; i++) {
            var id = String(cards[i].getAttribute('data-task-id'));
            if (claimedTicketIds[id]) {
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
                badge.className = active > 0 ? 'active' : 'idle';
                label.textContent = '🔌 ' + connected + ' connected · 🤖 '
                    + active + ' working';
                if (connected === 0 && active === 0) {
                    tooltip.innerHTML = 'No AI agents connected right now.';
                } else {
                    // agent_id / ticket_id / usage are agent-controlled and go
                    // into innerHTML — escape every interpolation to prevent XSS.
                    var rows = agents.map(function (a) {
                        var u = a.usage;
                        var usageStr = '';
                        if (u && (u.used != null || u.limit != null)) {
                            var lim = (u.limit == null) ? '&#8734;' : mEsc(u.limit);
                            usageStr = '&nbsp;&mdash;&nbsp;usage '
                                + (u.used == null ? '?' : mEsc(u.used))
                                + ' / ' + lim + (u.unit ? ' ' + mEsc(u.unit) : '');
                        }
                        return '&#x25B6; Ticket&nbsp;<strong>#' + mEsc(a.ticket_id)
                            + '</strong>&nbsp;&mdash;&nbsp;' + mEsc(a.agent_id) + usageStr;
                    });
                    tooltip.innerHTML =
                        '<strong>' + connected + '</strong> connected, <strong>'
                        + active + '</strong> working<br>'
                        + (rows.length ? rows.join('<br>') : 'No claimed tickets.');
                }
                // Rebuild the claimed set from data.agents, restricted to
                // tickets actually in In Progress right now, and repaint
                // the golden rings.
                claimedTicketIds = Object.create(null);
                agents.forEach(function (a) {
                    if (a.state === 'in_progress') {
                        claimedTicketIds[String(a.ticket_id)] = true;
                    }
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
    // which rebuilds the card DOM and would drop our class (and the inline
    // column-scroll-height style — same rebuild problem, same fix). Re-apply
    // both from cache whenever the board subtree changes. Debounced so a
    // burst of mutations triggers a single repaint.
    (function () {
        var boardEl = document.getElementById('board') || document.body;
        if (typeof MutationObserver === 'undefined') { return; }
        var scheduled = false;
        var obs = new MutationObserver(function () {
            if (scheduled) { return; }
            scheduled = true;
            setTimeout(function () {
                scheduled = false;
                applyAgentBorders();
                applyColumnScrollHeights();
            }, 100);
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

    /* ── Project-level main-branch preview (Start/Stop) ─────────────────
       Deploys the project's `main` branch instead of a ticket's branch —
       distinct from the per-ticket Start/Stop Preview buttons in the task
       sidebar. "Start Main Preview" is a plain <a target="_blank">
       navigation to a NEW tab, so this tab gets no click event and no
       signal that a preview was started — starting one doesn't touch any
       Kanboard board/ticket state, so it never fires the EventSource
       "refresh" push either. Polling STATUS_URL is what actually notices
       the state change and swaps the buttons in, without requiring a
       manual page reload. lastState dedupes so an unchanged poll result
       never re-renders (no flicker, no risk of clobbering an in-flight
       "Stopping…" click). */
    (function () {
        var wrap = document.getElementById('marcus-main-preview-wrap');
        if (!wrap || !PROJECT_ID) { return; }

        var POLL_MS = 4000;
        var lastState = null; // null (unknown yet) | 'running' | 'stopped' | 'stopping'

        function renderStopped() {
            lastState = 'stopped';
            wrap.innerHTML =
                '<a href="' + DEV_ENV_MAIN_VIEW_URL + '" target="_blank" '
                + 'rel="noopener noreferrer" class="marcus-main-preview-btn start">'
                + '&#128064; Start Main Preview'
                + '</a>';
        }

        function renderRunning(previewUrl) {
            lastState = 'running';
            wrap.innerHTML =
                '<a href="' + mEsc(previewUrl) + '" target="_blank" '
                + 'rel="noopener noreferrer" class="marcus-main-preview-btn open">'
                + '&#127758; Open Main Preview'
                + '</a>'
                + '<a href="' + DEV_ENV_MAIN_LOGS_URL + '" target="_blank" '
                + 'rel="noopener noreferrer" class="marcus-main-preview-btn logs" '
                + 'id="marcus-main-logs-btn">'
                + '&#128220; View Logs'
                + '</a>'
                + '<button class="marcus-main-preview-btn stop" id="marcus-main-stop-btn">'
                + '&#9632; Stop Main Preview'
                + '</button>';

            document.getElementById('marcus-main-stop-btn').addEventListener('click', function () {
                this.disabled = true;
                this.textContent = 'Stopping…';
                lastState = 'stopping'; // hold the poll off while this is in flight
                fetch(DEV_ENV_MAIN_STOP_URL, {
                    method: 'POST', cache: 'no-store', headers: marcusHeaders(),
                })
                    .then(function (r) { return r.json(); })
                    .then(function () { renderStopped(); })
                    .catch(function () { renderStopped(); });
            });
        }

        function checkStatus() {
            if (lastState === 'stopping') { return; } // a stop click owns the UI right now
            fetch(DEV_ENV_MAIN_STATUS_URL, { cache: 'no-store', headers: marcusHeaders() })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    // Re-check here, not just before issuing the fetch above:
                    // a poll already in flight when Stop is clicked resolves
                    // with STALE data (still running:true) after lastState has
                    // since moved to 'stopping' — without this second check
                    // that stale response would call renderRunning() and
                    // silently replace the "Stopping…" button with a fresh,
                    // enabled one while the actual stop request is still
                    // pending server-side.
                    if (lastState === 'stopping') { return; }
                    var running = !!(data.running && data.url);
                    var newState = running ? 'running' : 'stopped';
                    if (newState === lastState) { return; } // nothing changed — skip re-render
                    if (running) { renderRunning(data.url); } else { renderStopped(); }
                })
                .catch(function () {
                    // Transient network hiccup: keep whatever is currently shown
                    // rather than flashing to "stopped" — except on the very
                    // first check, where SOMETHING must render.
                    if (lastState === null) { renderStopped(); }
                });
        }

        checkStatus();
        setInterval(checkStatus, POLL_MS);
    }());

    /* ── Clone this project ──────────────────────────────────────────────
       Creates a brand-new Kanboard project (+ Gitea repo) that replicates
       every ticket (title, description, column, labels, links), the
       project description, and gate/access settings of THIS project —
       see src/workflows/project_clone_workflow.py. The clone runs in the
       background on Marcus (it can take a while: mirror-cloning a git
       repo and recreating every ticket), so the click starts a job and
       this polls /api/clone-project-status instead of blocking. */
    (function () {
        var statusEl = document.getElementById('marcus-clone-status');
        if (!PROJECT_ID || !statusEl) { return; }

        // ~10 minutes of 2s polling before giving up on the POLL (not the
        // clone itself, which keeps running server-side regardless).
        var MAX_POLL_ATTEMPTS = 300;

        function poll(jobId, attempt) {
            fetch(CLONE_PROJECT_STATUS_URL + '?job_id=' + encodeURIComponent(jobId), {
                cache: 'no-store', headers: marcusHeaders(),
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data.status === 'running') {
                        if (attempt >= MAX_POLL_ATTEMPTS) {
                            statusEl.textContent = 'Still cloning… check back later.';
                            return;
                        }
                        setTimeout(function () { poll(jobId, attempt + 1); }, 2000);
                        return;
                    }
                    if (data.status === 'done') {
                        var warnCount = (data.warnings || []).length;
                        // Kanboard's own board route is board/<project_id> —
                        // window.location.origin is always Kanboard's own
                        // origin here, since this script only ever runs
                        // inside a Kanboard-served page.
                        var boardUrl = window.location.origin + '/board/' + encodeURIComponent(data.new_project_id);
                        statusEl.innerHTML = 'Clone complete — <a href="' + mEsc(boardUrl) + '" target="_blank" rel="noopener noreferrer">open project #' + mEsc(data.new_project_id) + '</a>'
                            + (warnCount ? ' (' + warnCount + ' warning(s), see Marcus logs)' : '') + '.';
                        return;
                    }
                    statusEl.textContent = 'Clone failed: ' + mEsc(data.error || 'unknown error');
                })
                .catch(function () {
                    statusEl.textContent = 'Lost contact with Marcus while cloning — it may still finish in the background.';
                });
        }

        window.cloneThisProject = function () {
            var name = window.prompt('Name for the cloned project:');
            if (!name) { return; }
            name = name.trim();
            if (!name) { return; }

            statusEl.textContent = 'Starting clone…';
            fetch(CLONE_PROJECT_URL, {
                method: 'POST',
                headers: marcusHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ baseline_project_id: PROJECT_ID, new_name: name }),
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data.job_id) {
                        statusEl.textContent = 'Could not start clone: ' + mEsc(data.error || 'unknown error');
                        return;
                    }
                    statusEl.textContent = 'Cloning…';
                    poll(data.job_id, 0);
                })
                .catch(function () {
                    statusEl.textContent = 'Could not reach Marcus to start the clone.';
                });
        };
    })();
}());
</script>
