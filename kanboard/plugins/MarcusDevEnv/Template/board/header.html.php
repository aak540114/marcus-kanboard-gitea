<?php
/**
 * MarcusDevEnv board header template — injected at the top of every board view.
 *
 * Renders a live "Active AI Agents" badge that polls the Marcus
 * /api/active-agents endpoint every 15 seconds and updates in place.
 *
 * The badge shows:
 *   - Count of AI agents currently working on tickets
 *   - A tooltip listing each ticket ID and the agent working on it
 *   - Green when agents are active, grey when none are working
 */
$marcusUrl = getenv('MARCUS_URL') ?: 'http://localhost:4298';
$apiUrl    = $marcusUrl . '/api/active-agents';
?>
<style>
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
    margin: 6px 0 4px 0;
    border: 1px solid transparent;
}
#marcus-agent-badge.active {
    background: #e6f4ea;
    color: #1a7f3c;
    border-color: #a8d5b5;
}
#marcus-agent-badge.idle {
    background: #f4f4f4;
    color: #888;
    border-color: #ddd;
}
#marcus-agent-badge.error {
    background: #fff3e0;
    color: #b45309;
    border-color: #f8c97a;
}
#marcus-agent-badge .badge-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
}
#marcus-agent-badge.active .badge-dot  { background: #1a7f3c; }
#marcus-agent-badge.idle  .badge-dot  { background: #aaa; }
#marcus-agent-badge.error .badge-dot  { background: #b45309; }

#marcus-agent-tooltip {
    display: none;
    position: absolute;
    z-index: 9999;
    background: #1e2533;
    color: #e8eaf0;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 12px;
    line-height: 1.6;
    white-space: nowrap;
    box-shadow: 0 4px 16px rgba(0,0,0,.25);
    pointer-events: none;
    margin-top: 4px;
}
#marcus-agent-badge:hover + #marcus-agent-tooltip,
#marcus-agent-badge:focus + #marcus-agent-tooltip {
    display: block;
}
</style>

<div style="padding: 0 16px 2px; position: relative; display: inline-block;">
    <span id="marcus-agent-badge" class="idle" title="">
        <span class="badge-dot"></span>
        <span id="marcus-agent-label">&#129302; Marcus: checking&hellip;</span>
    </span>
    <div id="marcus-agent-tooltip"></div>
</div>

<script>
(function () {
    var API_URL   = <?= json_encode($apiUrl) ?>;
    var INTERVAL  = 15000; // ms between polls
    var badge     = document.getElementById('marcus-agent-badge');
    var label     = document.getElementById('marcus-agent-label');
    var tooltip   = document.getElementById('marcus-agent-tooltip');

    function update() {
        fetch(API_URL, { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var count  = data.active_agent_count || 0;
                var agents = data.agents || [];

                badge.className = count > 0 ? 'active' : 'idle';

                if (count === 0) {
                    label.textContent = '🤖 Marcus: no active agents';
                    tooltip.innerHTML  = 'No AI agents are working right now.';
                } else {
                    label.textContent = '🤖 Marcus: ' + count
                        + (count === 1 ? ' agent active' : ' agents active');

                    var rows = agents.map(function (a) {
                        return '&#x25B6; Ticket&nbsp;<strong>#' + a.ticket_id
                            + '</strong>&nbsp;&mdash;&nbsp;' + a.agent_id;
                    });
                    tooltip.innerHTML = rows.join('<br>');
                }
            })
            .catch(function () {
                badge.className   = 'error';
                label.textContent = '🤖 Marcus: unreachable';
                tooltip.innerHTML = 'Could not reach Marcus at<br>' + API_URL;
            });
    }

    // First fetch immediately, then poll
    update();
    setInterval(update, INTERVAL);
}());
</script>
