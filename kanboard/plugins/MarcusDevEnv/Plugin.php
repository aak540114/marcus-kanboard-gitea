<?php

namespace Kanboard\Plugin\MarcusDevEnv;

use Kanboard\Core\Plugin\Base;

/**
 * MarcusDevEnv — Kanboard plugin
 *
 * Adds a "View Live Changes" sidebar button to every task, plus a row of
 * Marcus controls (active-agents badge, project description link, gate
 * toggle, AI-verify counter, max-dev-envs counter) injected via the
 * project header, which is shared by every project-scoped view (board,
 * list, calendar, Gantt, search) — so it isn't limited to the board.
 *
 * Configuration
 * -------------
 * Set the environment variable MARCUS_URL to the base URL of your Marcus
 * MCP server (e.g. http://localhost:4298). Falls back to that default.
 */
class Plugin extends Base
{
    /**
     * Called by Kanboard when the plugin is loaded.
     */
    public function initialize(): void
    {
        // Relax Kanboard's Content-Security-Policy so this plugin's
        // browser code can actually run. Kanboard's default CSP
        // (app/ServiceProvider/ClassProvider.php) is:
        //   default-src 'self'; style-src 'self' 'unsafe-inline'; img-src * data:
        // There is NO script-src, so it falls back to default-src 'self' —
        // which blocks BOTH the inline <script> in the header/sidebar
        // templates AND the inline onclick= handlers on the gate/verify/
        // dev-env buttons. Symptom: the agent badge renders (styles are
        // allowed) but stays on "checking…" forever because updateAgents()
        // never executes; the gate toggle appears inert.
        //
        // Two directives are needed:
        //   script-src 'self' 'unsafe-inline'  → run the inline JS + handlers
        //   connect-src 'self' <marcus-origin> → allow the cross-origin
        //       fetch() to Marcus (a different port = a different origin;
        //       connect-src otherwise falls back to default-src 'self').
        //
        // setContentSecurityPolicy() is Kanboard's documented plugin hook
        // for this (Core\Plugin\Base). Acceptable on this single-admin
        // local/demo stack; 'unsafe-inline' for scripts does weaken the
        // app-wide CSP, so on a hardened multi-user deployment prefer
        // moving the template JS to an external asset instead.
        $marcusUrl = getenv('MARCUS_URL') ?: 'http://localhost:4298';
        $parts = parse_url($marcusUrl);
        $marcusOrigin = '';
        if (is_array($parts) && !empty($parts['scheme']) && !empty($parts['host'])) {
            $marcusOrigin = $parts['scheme'] . '://' . $parts['host'];
            if (!empty($parts['port'])) {
                $marcusOrigin .= ':' . $parts['port'];
            }
        }
        $this->setContentSecurityPolicy(array(
            'default-src' => "'self'",
            'style-src'   => "'self' 'unsafe-inline'",
            'script-src'  => "'self' 'unsafe-inline'",
            'connect-src' => trim("'self' " . $marcusOrigin),
            'img-src'     => '* data:',
        ));

        $this->template->hook->attach(
            'template:task:sidebar:information',
            'MarcusDevEnv:task/sidebar'
        );
        // 'template:board:private:header' does not exist in Kanboard (verified
        // against app/Template/board/view_private.php and table_container.php,
        // both on master and the v1.2.52 release tag actually shipped by the
        // kanboard/kanboard:latest Docker image — neither fires any hook near
        // the board header). 'template:project:header:after' is the real hook
        // fired at the end of app/Template/project_header/header.php, which
        // every project-scoped view renders — this is what actually reaches
        // the page.
        $this->template->hook->attach(
            'template:project:header:after',
            'MarcusDevEnv:board/header'
        );
    }

    /**
     * Plugin metadata shown in the Kanboard plugin manager.
     */
    public function getPluginName(): string
    {
        return 'Marcus Dev Environment';
    }

    public function getPluginDescription(): string
    {
        return 'Adds a "View Live Changes" button to each task that spins up a hot-reload dev environment via Marcus.';
    }

    public function getPluginAuthor(): string
    {
        return 'Marcus';
    }

    public function getPluginVersion(): string
    {
        return '1.0.0';
    }

    public function getPluginHomepage(): string
    {
        return 'https://github.com/aak540114/marcus';
    }
}
